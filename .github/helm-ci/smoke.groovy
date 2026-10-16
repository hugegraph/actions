// Licensed to the Apache Software Foundation (ASF) under one or more
// contributor license agreements. See the NOTICE file distributed with
// this work for additional information regarding copyright ownership.
// The ASF licenses this file to You under the Apache License, Version 2.0
// (the "License"); you may not use this file except in compliance with
// the License. You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

// Trusted CI-only script. The Helm hook retries the complete exercise phase.
// Later requests verify committed data before/after Pod recreation, without
// creating schema or repairing missing data. Cleanup touches only this run's IDs.
def run = '__HELM_CI_RUN__';
def phase = '__HELM_CI_PHASE__';
assert phase in ['exercise', 'verify', 'cleanup'] : "Unknown smoke phase: ${phase}";
def label = 'helm_ci_smoke_v2';
def edgeLabel = 'helm_ci_link_v2';
def textKey = 'helm_ci_value_v2';
def numberKey = 'helm_ci_number_v2';
def indexName = 'helm_ci_value_idx_v2';
def a = run + '-a';
def b = run + '-b';
def rolledBack = run + '-rollback';
def g = graph.traversal();
def schema = graph.schema();
def check = { condition, message ->
    assert condition : "helm-ci ${phase}: ${message}";
};
def indexedIds = { value ->
    // Both predicates are required: this uses the secondary index and never
    // scans application vertices or requires an unbounded whole-graph count.
    g.V().hasLabel(label).has(textKey, value).id().toList().collect { it.toString() } as Set;
};
def clean = {
    graph.tx().rollback();
    g.V(a, b, rolledBack).drop().iterate();
    graph.tx().commit();
    check(!g.V(a, b, rolledBack).hasNext(), 'run-specific vertices were not deleted');
};
def checkSchema = {
    def text = schema.getPropertyKey(textKey);
    def number = schema.getPropertyKey(numberKey);
    def vertex = schema.getVertexLabel(label);
    def edge = schema.getEdgeLabel(edgeLabel);
    def index = schema.getIndexLabel(indexName);
    check(text.dataType().name() == 'TEXT', 'text property schema differs');
    check(number.dataType().name() == 'INT', 'integer property schema differs');
    check(vertex.idStrategy().name() == 'CUSTOMIZE_STRING', 'vertex ID strategy differs');
    check(vertex.properties() as Set == [text.id(), number.id()] as Set,
          'vertex property schema differs');
    check(edge.sourceLabel() == vertex.id() && edge.targetLabel() == vertex.id(),
          'edge endpoints differ');
    check(edge.properties() as Set == [number.id()] as Set, 'edge property schema differs');
    check(index.indexType().name() == 'SECONDARY' && index.baseValue() == vertex.id() &&
          index.indexFields() == [text.id()], 'secondary index schema differs');
    // Re-read by ID after the name lookup refreshes metadata. A pending index
    // fails this attempt; the existing bounded Helm hook retry handles startup.
    def currentIndex = graph.indexLabel(index.id());
    check(currentIndex.status().ok(),
          "secondary index is ${currentIndex.status()} (CREATED expected)");
};
def addPair = { firstValue, secondValue ->
    def first = graph.addVertex(org.apache.tinkerpop.gremlin.structure.T.id, a,
                               org.apache.tinkerpop.gremlin.structure.T.label, label,
                               textKey, firstValue, numberKey, 11);
    def second = graph.addVertex(org.apache.tinkerpop.gremlin.structure.T.id, b,
                                org.apache.tinkerpop.gremlin.structure.T.label, label,
                                textKey, secondValue, numberKey, 22);
    first.addEdge(edgeLabel, second, numberKey, 33);
    graph.tx().commit();
};
def verifySentinel = {
    checkSchema();
    check(g.V(a, b).count().next() == 2, 'committed sentinel vertices are missing');
    check(g.V(a).hasLabel(label).has(textKey, run + '-sentinel-a')
           .has(numberKey, 11).hasNext(), 'first sentinel properties differ');
    check(g.V(b).hasLabel(label).has(textKey, run + '-sentinel-b')
           .has(numberKey, 22).hasNext(), 'second sentinel properties differ');
    check(g.V(a).outE(edgeLabel).count().next() == 1, 'sentinel edge count differs');
    check(g.V(a).outE(edgeLabel).has(numberKey, 33).inV().id().toList().collect { it.toString() } == [b],
          'sentinel edge target/properties differ');
    check(g.V(b).in(edgeLabel).id().toList().collect { it.toString() } == [a], 'sentinel incoming adjacency differs');
    check(indexedIds(run + '-sentinel-a') == [a] as Set,
          'first sentinel secondary index differs');
    check(indexedIds(run + '-sentinel-b') == [b] as Set,
          'second sentinel secondary index differs');
    check(!g.V(rolledBack).hasNext(), 'rolled-back vertex exists');
};

if (phase == 'cleanup') {
    clean();
    graph.tx().rollback();
} else if (phase == 'verify') {
    try {
        verifySentinel();
    } finally {
        graph.tx().rollback();
    }
} else {
    try {
        // Retry cleanup precedes schema creation so earlier partial attempts
        // cannot mask missing writes, stale indexes or incorrect deletions.
        clean();
        schema.propertyKey(textKey).asText().ifNotExist().create();
        schema.propertyKey(numberKey).asInt().ifNotExist().create();
        schema.vertexLabel(label).useCustomizeStringId()
              .properties(textKey, numberKey).ifNotExist().create();
        schema.edgeLabel(edgeLabel).sourceLabel(label).targetLabel(label)
              .properties(numberKey).ifNotExist().create();
        schema.indexLabel(indexName).onV(label).secondary().by(textKey)
              .ifNotExist().create();
        checkSchema();

        addPair(run + '-created-a', run + '-created-b');
        check(g.V(a, b).count().next() == 2, 'vertex create/read failed');
        check(g.V(a).values(numberKey).next() == 11 &&
              g.V(b).values(numberKey).next() == 22, 'integer property read failed');
        check(g.V(a).out(edgeLabel).id().toList().collect { it.toString() } == [b] &&
              g.V(b).in(edgeLabel).id().toList().collect { it.toString() } == [a], 'directed edge traversal failed');
        check(indexedIds(run + '-created-a') == [a] as Set,
              'secondary index lookup after insert failed');

        g.V(a).property(textKey, run + '-updated-a').property(numberKey, 12).iterate();
        g.V(a).outE(edgeLabel).property(numberKey, 34).iterate();
        graph.tx().commit();
        check(g.V(a).values(textKey).next() == run + '-updated-a' &&
              g.V(a).values(numberKey).next() == 12, 'vertex update/read failed');
        check(g.V(a).outE(edgeLabel).values(numberKey).toList() == [34],
              'edge update/read failed');
        check(indexedIds(run + '-created-a').isEmpty(), 'old secondary index entry remains');
        check(indexedIds(run + '-updated-a') == [a] as Set,
              'updated secondary index entry is missing');

        graph.addVertex(org.apache.tinkerpop.gremlin.structure.T.id, rolledBack,
                        org.apache.tinkerpop.gremlin.structure.T.label, label,
                        textKey, run + '-rollback', numberKey, 99);
        g.V(a).property(numberKey, 99).iterate();
        g.V(a).outE(edgeLabel).property(numberKey, 99).iterate();
        graph.tx().rollback();
        check(!g.V(rolledBack).hasNext(), 'rolled-back insert persisted');
        check(indexedIds(run + '-rollback').isEmpty(), 'rolled-back index entry persisted');
        check(g.V(a).values(numberKey).next() == 12, 'rolled-back vertex update persisted');
        check(g.V(a).outE(edgeLabel).values(numberKey).toList() == [34],
              'rolled-back edge update persisted');

        g.V(a).outE(edgeLabel).drop().iterate();
        graph.tx().commit();
        check(!g.V(a).outE(edgeLabel).hasNext() && !g.V(b).inE(edgeLabel).hasNext(),
              'edge delete persisted in adjacency');
        clean();
        check(indexedIds(run + '-updated-a').isEmpty() &&
              indexedIds(run + '-created-b').isEmpty(), 'deleted vertex index entries remain');

        // Retain only this committed pair for independent HTTP readback and
        // persistence checks. Verification never repairs a missing sentinel.
        addPair(run + '-sentinel-a', run + '-sentinel-b');
        verifySentinel();
    } catch (Throwable failure) {
        try {
            clean();
        } catch (Throwable cleanupFailure) {
            failure.addSuppressed(cleanupFailure);
        }
        throw failure;
    } finally {
        graph.tx().rollback();
    }
}
"helm-ci ${phase} passed".toString();
