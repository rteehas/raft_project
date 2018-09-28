from enum import Enum


class State(Enum):
    """
    State Enum class to represent the state the node is in
    """
    LEADER = 0
    FOLLOWER = 1
    CANDIDATE = 2
