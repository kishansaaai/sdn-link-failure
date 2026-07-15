import sys
import os
import pytest
from unittest.mock import MagicMock

# Mock pox modules before importing link_failure_recovery
sys.modules['pox'] = MagicMock()
sys.modules['pox.core'] = MagicMock()
sys.modules['pox.openflow'] = MagicMock()
sys.modules['pox.openflow.libopenflow_01'] = MagicMock()
sys.modules['pox.lib'] = MagicMock()
sys.modules['pox.lib.revent'] = MagicMock()

# Add project root to sys.path to import link_failure_recovery
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from link_failure_recovery import TopologyGraph

@pytest.fixture
def graph():
    g = TopologyGraph()
    # Simple mesh:
    # 1 - 2 - 3
    # |       |
    # 4 - 5 - 6
    g.add_link(1, 1, 2, 1)
    g.add_link(2, 2, 3, 1)
    g.add_link(1, 2, 4, 1)
    g.add_link(4, 2, 5, 1)
    g.add_link(5, 2, 6, 1)
    g.add_link(3, 2, 6, 2)
    return g

def test_shortest_path_exists(graph):
    # Shortest path from 1 to 6 should be 1-2-3-6 or 1-4-5-6 (length 4)
    # Both have 3 hops (4 nodes)
    path = graph.dijkstra(1, 6)
    assert path is not None
    assert len(path) == 4
    assert path[0] == 1
    assert path[-1] == 6

def test_path_changes_after_edge_removal(graph):
    # Initial path
    path1 = graph.dijkstra(1, 6)
    
    # Remove an edge from the path (e.g. 2-3)
    graph.remove_link(2, 3)
    
    path2 = graph.dijkstra(1, 6)
    assert path2 is not None
    assert len(path2) == 4 # still 4 nodes via 1-4-5-6
    # verify it uses the other route
    assert 3 not in path2
    assert 4 in path2 and 5 in path2

def test_no_path_exists(graph):
    # Disconnect graph into two components
    graph.remove_link(1, 2)
    graph.remove_link(1, 4)
    
    # Node 1 is completely isolated
    path = graph.dijkstra(1, 6)
    assert path is None

def test_path_recomputes_after_edge_readded(graph):
    # Disconnect 1 from 6 completely
    graph.remove_link(1, 2)
    graph.remove_link(1, 4)
    
    assert graph.dijkstra(1, 6) is None
    
    # Add a direct link between 1 and 6
    graph.add_link(1, 3, 6, 3)
    
    path = graph.dijkstra(1, 6)
    assert path is not None
    assert len(path) == 2
    assert path == [1, 6]
