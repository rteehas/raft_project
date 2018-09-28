# raft_project
Final group project for my senior year Advanced Distributed Systems course (http://uchicago-cs.github.io/cmsc23310/index.html) taught by Professor Ian Foster. My group decided to implement the RAFT algorithm for the system that we built. Aninitial breakdown of the work can be found below.

===== Names =====
Ji Seung "Anna" Kim (jiseung@uchicago.edu)
Lucy Newman (newmanlucy@uchicago.edu)
Ryan Teehan (rteehan@uchicago.edu)



===== Roles =====
Anna:
write test scripts and testing
debugging
writing report

Lucy:
initial implementation of log replication
debugging
writing report

Ryan:
initial implementation of leader election
debugging
writing report



===== Chistributed instructions =====
You can run chistributed within impl directory.
You may use the test scripts with extension .in provided in impl/scripts.
You may look at test scripts with extension .test for the specific scenario
that we are testing, but you must substitute [leader] with the appropriate
leader node name. The leader node name will be given upon a client request
by any node that isn't the leader.



===== Files =====
node.py:
The copy of the program that each server will receive in chistributed.
Specifies the Node class which implements a Raft server.

state.py:
Enum class used by a Node to specify whether
it's a follower, candidate, or leader.

chistributed.conf:
Specifies all the nodes that will be started in chistributed.
Our implementation will only begin to accept client requests once
all the nodes in this file is started.
