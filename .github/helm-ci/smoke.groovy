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

// Trusted CI-only script. The chart hook retries the complete script.
// Exact ID lookups avoid requiring an index or scanning application vertices.
def id = '__HELM_CI_RUN__';
def label = 'helm_ci_smoke';
def key = 'helm_ci_value';
def g = graph.traversal();
graph.schema().propertyKey(key).asText().ifNotExist().create();
graph.schema().vertexLabel(label).useCustomizeStringId()
     .properties(key).ifNotExist().create();
try {
    // Clean an earlier partial attempt before testing this attempt.
    g.V(id).drop().iterate();
    graph.tx().commit();
    graph.addVertex(org.apache.tinkerpop.gremlin.structure.T.id, id,
                    org.apache.tinkerpop.gremlin.structure.T.label, label,
                    key, 'created');
    graph.tx().commit();
    assert g.V(id).values(key).next() == 'created';
    g.V(id).property(key, 'updated').iterate();
    graph.tx().commit();
    assert g.V(id).values(key).next() == 'updated';
    g.V(id).drop().iterate();
    graph.tx().commit();
    assert !g.V(id).hasNext();
} finally {
    graph.tx().rollback();
    g.V(id).drop().iterate();
    graph.tx().commit();
}
'helm-ci CRUD passed';
