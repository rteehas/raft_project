import json
import sys
import signal
import zmq
import time
import click
from random import uniform

import colorama
from zmq.eventloop import ioloop, zmqstream

from state import State

colorama.init()
ioloop.install()


# Represent a node in our data store
class Node(object):

    ##########################################################################

    #                       RAFT CONSTANTS                                   #

    ##########################################################################

    # election timeout lower and upper bounds, can be changed
    lb = .5
    ub = 1.5

    # timeout for restarting election
    election_timeout = 1

    # heartbeat times
    heartbeat_frequency = 1
    heartbeat_timeout = 2

    # get/set hop timeout
    max_forwards = 3

    ##########################################################################

    #                       ZMQ NODE FUNCTIONS                               #

    ##########################################################################

    def __init__(
            self,
            node_name,
            pub_endpoint,
            router_endpoint,
            peer_names,
            debug
            ):
        """
        Initialize the node
        """
        self.loop = ioloop.IOLoop.instance()
        self.context = zmq.Context()

        self.connected = False

        # SUB socket for receiving messages from the broker
        self.sub_sock = self.context.socket(zmq.SUB)
        self.sub_sock.connect(pub_endpoint)

        # Make sure we get messages meant for us!
        self.sub_sock.setsockopt_string(zmq.SUBSCRIBE, node_name)

        # Create handler for SUB socket
        self.sub = zmqstream.ZMQStream(self.sub_sock, self.loop)
        self.sub.on_recv(self.handle)

        # REQ socket for sending messages to the broker
        self.req_sock = self.context.socket(zmq.REQ)
        self.req_sock.connect(router_endpoint)
        self.req_sock.setsockopt_string(zmq.IDENTITY, node_name)

        # We don't strictly need a message handler for the REQ socket,
        # but we define one in case we ever receive any errors through
        # it.
        self.req = zmqstream.ZMQStream(self.req_sock, self.loop)
        self.req.on_recv(self.handle_broker_message)

        self.name = node_name
        self.peer_names = peer_names

        self.debug = debug

        # Capture signals to ensure an orderly shutdown
        for sig in [
            signal.SIGTERM,
            signal.SIGINT,
            signal.SIGHUP,
            signal.SIGQUIT
                ]:
            signal.signal(sig, self.shutdown)

        self.initialize_node()

    def start(self):
        """
        Starts the ZeroMQ loop
        """
        self.loop.start()

    def shutdown(self, sig, frame):
        """
        Performs an orderly shutdown
        """
        self.loop.stop()
        self.sub_sock.close()
        self.req_sock.close()
        sys.exit(0)

    ##########################################################################

    #                          LOGGING FUNCTIONS                             #

    ##########################################################################

    def log(self, msg):
        """
        Log with bright colorama style
        """
        log_msg = ">>> %10s -- %s" % (self.name, msg)
        print(colorama.Style.BRIGHT + log_msg + colorama.Style.RESET_ALL)

    def log_debug(self, msg):
        """
        Log with blue colorama style
        """
        if self.debug:
            log_msg = ">>> %10s -- %s" % (self.name, msg)
            print(colorama.Fore.BLUE + log_msg + colorama.Style.RESET_ALL)

    ##########################################################################

    #                      RAFT NODE INITIALIZATION                          #

    ##########################################################################

    def initialize_node(self):
        """
        Initialize Raft-related Node attributes
        """
        # The node's data store and a log of entries that are and aren't
        # committed
        self.store = {}
        self.store_log = []

        # Related to state and state transition
        self.timeout_object = None
        self.state = None

        # for voting
        self.accs = set()
        self.refs = set()

        # persistent state
        self.term = 0
        self.voted_for = {}
        self.started_peers = []

        # volatile state on all servers
        self.last_applied = 0
        self.timeout_object = None

        # volatile state on leaders
        self.next_index = {}
        self.match_index = {}
        self._prev_committed_index = 0
        self.highest_committed_index = 0
        self.leader_confirmed = False
        self.received_from = set()

        # volatile state for followers
        self.received_heartbeat = False
        self.current_leader = None
        self.follower_committed_index = 0
        self.follower_last_log_applied = 0
        self.vote = None

    ##########################################################################

    #                             PROPERTIES                                 #

    ##########################################################################

    @property
    def all_nodes_started(self):
        """
        Whether all nodes have joined the network
        """
        return len(self.received_from) == len(self.peer_names)

    @property
    def majority(self):
        """
        The number of replies that must be exceeded to get a majority
        """
        return ((len(self.peer_names) + 1) / 2)

    @property
    def last_log_index(self):
        """
        Index of the last node in the log
        """
        if self.store_log == []:
            return 0
        return self.store_log[-1]['index']

    @property
    def last_log_term(self):
        """
        Term of the last node in the log
        """
        if self.store_log == []:
            return 0
        return self.store_log[-1]['term']

    @property
    def uncompacted_log_len(self):
        """
        Length of the uncompacted log
        """
        return len(self.store_log)

    @property
    def leader_committed_index(self):
        """
        Committed index from the leader
        """
        # self.log_debug(self.match_index)
        if self.match_index == {}:
            return 0
        histogram = {}
        for node in self.match_index:
            node_val = self.match_index[node]
            for val in range(self._prev_committed_index, node_val + 1):
                if val in histogram:
                    histogram[val] += 1
                else:
                    histogram[val] = 1
        # self.log_debug(histogram)
        new_committed_index = self._prev_committed_index
        for val in histogram:
            if histogram[val] > self.majority - 1:
                new_committed_index = val
            else:
                break
        self._prev_committed_index = new_committed_index
        return new_committed_index

    @property
    def num_peers(self):
        """
        Number of peers on the network
        """
        return len(self.peer_names)

    @property
    def commit_index(self):
        """
        Commit index of a node
        """
        # using follower commit if we just changed leaders
        if self.follower_committed_index > self.leader_committed_index:
            leader_commit = self.follower_committed_index
        else:
            leader_commit = self.leader_committed_index
        return leader_commit

    ##########################################################################

    #                              UTILITIES                                 #

    ##########################################################################

    def remove_timer(self):
        """
        Remove the timer
        """
        if self.timeout_object is not None:
            self.loop.remove_timeout(self.timeout_object)

    def reset_voting(self):
        """
        Reset voting
        """
        self.accs = set()
        self.refs = set()

    def update_term_and_follow(self, msg):
        """
        Update term and follow new leader if their term is greater than yours
        """
        if 'term' in msg and msg['term'] > self.term:
            self.term = msg['term']
            self.transition_state(State.FOLLOWER, new_leader=msg['source'])
        if self.term == msg['term'] and self.current_leader == msg['source']:
            self.start_follower_timeout()

    def make_new_log_entry(self, leader, key, value, msg_id):
        """
        Construct a log entry
        """
        entry = {
            'term': self.term,
            'index': self.last_log_index + 1,
            'leader': leader,
            'key': key,
            'value': value,
            'id': msg_id
            }
        return entry

    ##########################################################################

    #                             STATE TRANSITION                           #

    ##########################################################################

    def transition_state(self, new_state, new_leader=None):
        """
        Transition states by removing old timer and calling the state-specific
        transition function
        """
        self.remove_timer()
        if new_state == State.LEADER:
            self.start_leader()
        elif new_state == State.CANDIDATE:
            self.start_candidate()
        elif new_state == State.FOLLOWER:
            self.start_follower(new_leader)

    def start_candidate(self):
        """
        Transition to Candidate state: start an election after waiting for a
        randomized amount of time
        """
        self.state = State.CANDIDATE
        noisy_wait = uniform(self.lb, self.ub)
        self.timeout_object = self.loop.call_later(
            noisy_wait,
            self.start_election
            )

    def initialize_indexes(self):
        """
        Initialize match_index and next_index for a new leader
        """
        self.next_index = {}
        self.match_index = {}
        for p in self.peer_names:
            self.next_index[p] = self.last_log_index
            self.match_index[p] = 0

    def start_leader(self):
        """
        Transition to Leader state: Initialize indexes, send out no-op, and
        start heartbest
        """
        self.state = State.LEADER
        self.initialize_indexes()
        self.highest_committed_index = self.follower_committed_index
        self.current_leader = self.name
        self.leader_confirmed = False

        new_entry = self.make_new_log_entry(self.name, None, None, None)
        self.store_log.append(new_entry)
        self.send_append_entries()
        self.timeout_object = self.startbeat()

    def start_follower_timeout(self):
        """
        Start or re-start timeout for follower
        """
        self.remove_timer()
        self.timeout_object = self.loop.call_later(
            self.heartbeat_timeout,
            self.start_candidate
            )

    def start_follower(self, new_leader):
        """
        Transition to follower state: Set current_leader an start follower
        timeout
        """
        self.state = State.FOLLOWER
        self.current_leader = new_leader
        self.start_follower_timeout()

    ##########################################################################

    #                           HELLO HANDLING                               #

    ##########################################################################

    def handle_hello(self, msg):
        """
        Handle hello messages with response to broker
        """
        # Only handle this message if we are not yet connected to the broker.
        # Otherwise, we should ignore any subsequent hello messages.
        if not self.connected:
            # Send helloResponse
            self.connected = True
            self.send_to_broker({'type': 'helloResponse', 'source': self.name})
            self.transition_state(State.CANDIDATE)

    ##########################################################################

    #          LOG REPLICATION AND KEEPALIVE (APPENDD ENTRIES RPC)          #

    ##########################################################################

    def startbeat(self):  # (see what I did there)
        """
        Start the heartbeat: send out append entries and start timer with
        same function as callback
        """
        self.send_append_entries()
        self.remove_timer()
        self.timeout_object = self.loop.call_later(
            self.heartbeat_frequency,
            self.startbeat
            )

    def send_append_entries(self):
        """
        Sends out the heartbeat
        """
        for node in self.peer_names:
            prev_log_index = self.next_index[node]
            if prev_log_index != self.last_log_index:
                entries = self.store_log[prev_log_index:]
                prev_log_term = self.store_log[prev_log_index]['term']
            else:
                entries = []
                prev_log_index = 0
                prev_log_term = 0

            msg = {
                'type': 'appendEntries',
                'source': self.name,
                'destination': node,
                'term': self.term,
                'leaderName': self.name,
                'prevLogIndex': prev_log_index,
                'prevLogTerm': prev_log_term,
                'entries': entries,
                'leaderCommit': self.commit_index
                }
            self.send_to_broker(msg)

    def delete_conflicting_log_entries(self, msg):
        """
        Delete log entries that conflict with the new ones in append entries
        and acknowledge overwites if any occured for which you were the
        original leader
        """
        i = 0
        for j in range(len(self.store_log)):
            log_entry = self.store_log[j]
            msg_entry = msg['entries'][i]
            if log_entry['index'] == msg_entry['index']:
                if log_entry['term'] != msg_entry['term']:
                    self.store_log = self.store_log[:j]
                    self.acknowledge_overwrites(self.store_log[j:])
                    break
                else:
                    i += 1
                    if i == len(msg['entries']):
                        return

    def add_entries_to_store_log(self, entries):
        """
        Add new entries to the log
        """
        self.store_log += entries

    def process_committed_entry(self, key, value):
        """
        For newly committed entries, if it is the initial no-op, confirm the
        leader. If they are a set, write the entry to the concise data store
        """
        if key is None:
            self.leader_confirmed = True
        if value is not None:
            self.store[key] = value

    def update_follower_committed_index(self, new_index):
        """
        Update the committed index of a follower
        """
        self.follower_committed_index = new_index
        if self.follower_last_log_applied < new_index:
            for entry in self.store_log:
                if entry['index'] > self.follower_last_log_applied:
                    self.process_committed_entry(entry['key'], entry['value'])

    def handle_append_entries(self, msg):
        """
        Handle append entries:
        - Update term and transition to follower if necessary
        - Update committed index
        - Delete conflicting log entries
        - Append new entries
        - Send acknowledgement
        """
        self.update_term_and_follow(msg)

        self.update_follower_committed_index(msg['leaderCommit'])

        if msg['entries'] == []:
            return

        self.delete_conflicting_log_entries(msg)

        self.add_entries_to_store_log(msg['entries'])

        reply = {
            'type': 'appendEntriesAck',
            'source': self.name,
            'destination': msg['source'],
            'term': msg['term'],
            'index': msg['entries'][-1]['index']
            }
        self.send_to_broker(reply)

    def acknowledge_entry(self, entry):
        """
        Acknowledge an entry.
        - If the key is None, it is a no-op, so do nothing
        - Elif the value is None it is a 'get' request, so acknowledge get
        - Else it is a set request, so acknowledge set
        """
        if entry['key'] is None:
            return
        if entry['value'] is None:
            self.acknowledge_get_request(entry['key'], entry['id'])
        else:
            self.acknowledge_set_request(entry)

    def handle_append_entries_ack(self, msg):
        """
        Handle append entries ack
        - Update match index
        - Update compact data store
        - Acknowledge entries if necessary
        """
        if self.state != State.LEADER or self.term != msg['term']:
            return

        src = msg['source']
        prev_committed_index = self.commit_index
        self.match_index[src] = msg['index']

        for entry in self.store_log:
            if entry['index'] > prev_committed_index \
                    and entry['index'] <= self.leader_committed_index:
                self.process_committed_entry(entry['key'], entry['value'])
            if entry['index'] > prev_committed_index \
                    and entry['index'] <= self.leader_committed_index:
                self.acknowledge_entry(entry)
        if msg['index'] == self.next_index[src] + 1:
            self.next_index[src] += 1

    ##########################################################################

    #                LEADER ELECTION (REQUEST VOTE RPC)                      #

    ##########################################################################

    def start_election(self):
        """
        Start election
        - reset voting
        - Increment term
        - Send RequestVote to all peers
        """
        if self.peer_names == []:
            self.start_leader()
        else:
            self.reset_voting()
            self.term += 1
            for outnode in self.peer_names:
                msg = {
                    'type': 'requestVote',
                    'source': self.name,
                    'destination': outnode,
                    'term': self.term,
                    'lastLogIndex': self.last_log_index,
                    'lastLogTerm': self.last_log_term
                    }
                self.send_to_broker(msg)

    def handle_request_vote(self, msg):
        """
        Handle request vote:
        - Ignore if term is less than current term
        - Send vote if you haven't already voted for another node and candidate
        is at least as updated as you
        """
        if msg['term'] < self.term:
            return
        if msg['term'] in self.voted_for:
            voted_for = self.voted_for[msg['term']]
        else:
            voted_for = None
        if voted_for in [None, msg['source']] \
                and msg['lastLogIndex'] >= self.last_log_index \
                and msg['lastLogTerm'] >= self.last_log_term:
            vote_granted = True
            self.voted_for[msg['term']] = msg['source']
        else:
            vote_granted = False

        reply = {
                'type': 'requestVoteReply',
                'source': self.name,
                'destination': msg['source'],
                'voteGranted': vote_granted,
                'term': msg['term']
            }

        self.send_to_broker(reply)

    def handle_vote_reply(self, msg):
        """
        Handle vote reply:
        - Ignore if you're not a candidate or the term is outdated
        - Add node to accs or refs depending on if the vote was granted
        - Set timer to restart election in case of split vote
        """
        src = msg['source']
        if self.state != State.CANDIDATE or msg['term'] < self.term:
            return
        if msg['voteGranted'] is True:
            self.accs.add(src)
            if len(self.accs) > self.majority - 1:
                self.transition_state(State.LEADER)
                return
        if msg['voteGranted'] is False:
            self.refs.add(src)
        if self.all_nodes_started:
            self.timeout_object = self.loop.call_later(
                self.election_timeout,
                self.start_election
                )

    ##########################################################################

    #                       HANDLING CLIENT REQUESTS                         #

    ##########################################################################

    def redirect_to_leader(self, msg, msg_type):
        """
        Redirect a message to the leader. Not used.
        """
        if self.current_leader is None:
            self.transition_state(State.CANDIDATE)
        else:
            msg['type'] = msg_type
            msg['destination'] = self.current_leader
            self.send_to_broker(msg)

    def handle_client_request(self, msg, msg_type, leader_callback):
        """
        Handle client request. Return error message if candidate or follower,
        or call callback if leader
        """
        if self.state == State.CANDIDATE:
            self.send_client_error(
                msg_type, msg['id'],
                "You have contacted a candidate. Please try again later."
                )

        if self.state == State.FOLLOWER:
            self.send_client_error(
                msg_type, msg['id'],
                "You have contacted a follower. Please resend your request to %s."
                % self.current_leader
                )

        if self.state == State.LEADER:
            if self.leader_confirmed is False:
                self.send_client_error(
                    msg_type, msg['id'],
                    "The leader is not yet confirmed. Please try again later."
                    )
            else:
                leader_callback(msg)

    def send_client_error(self, msg_type, msg_id, error_msg):
        """
        Send an error response to the client
        """
        self.send_to_broker({
            'type': msg_type,
            'id': msg_id,
            'error': error_msg
            })

    def handle_get_from_leader(self, msg):
        """
        Handle get request from leader: Log entry with request as key and None
        as value and send out AppendEntries
        """
        new_entry = self.make_new_log_entry(
            self.name, msg['key'],
            None,
            msg['id']
            )
        self.store_log.append(new_entry)
        self.send_append_entries()

    def handle_get(self, msg):
        """
        Handle get request through handle_client_request
        """
        self.handle_client_request(
            msg,
            'getResponse',
            self.handle_get_from_leader
            )

    def acknowledge_get_request(self, key, msg_id):
        """
        Acknowledge get request: Send getResponse to broker if the value is in
        the data store, or send error message that it is not
        """
        time.sleep(0.1)
        if key in self.store:
            v = self.store[key]
            self.send_to_broker({
                'type': 'getResponse',
                'id': msg_id,
                'key': key,
                'value': v
                })
        else:
            self.send_client_error(
                'getResponse',
                msg_id,
                "No such key: %s" % key
                )

    def handle_set_from_leader(self, msg):
        """
        Handle set request from leader: add new entry to store and send
        AppendEntries call
        """
        k = msg['key']
        v = msg['value']

        # Simulate that storing data takes time
        time.sleep(0.1)

        new_entry = self.make_new_log_entry(self.name, k, v, msg['id'])
        self.store_log.append(new_entry)

        self.send_append_entries()

    def handle_set(self, msg):
        """
        Handle set request through handle_client_request
        """
        self.handle_client_request(
            msg,
            'setResponse',
            self.handle_set_from_leader
            )

    def acknowledge_set_request(self, entry):
        """
        Acknowledge set request: send setResponse to broker for the given
        entry's key, value, and ID
        """
        ack = {
            'type': 'setResponse',
            'id': entry['id'],
            'index': entry['index'],
            'key': entry['key'],
            'value': entry['value']
            }
        self.send_to_broker(ack)

    def acknowledge_overwrites(self, entries):
        """
        Acknowledge entries that have been overwritten. Only acknowledge your
        own writes to avoid duplicates
        """
        for entry in entries:
            if entry['key'] is None:
                continue
            elif entry['value'] is None:
                msg_type = 'getResponse'
            else:
                msg_type = 'setResponse'
            if entry['leader'] == self.name:
                self.send_client_error(
                    msg_type,
                    entry['id'],
                    "Your request has failed. Please try again."
                    )

    ##########################################################################

    #                       MESSAGE HANDLING                                 #

    ##########################################################################

    # Handle replies received through the REQ socket. Typically,
    # this will just be an acknowledgement of the message sent to the
    # broker through the REQ socket, but can also be an error message.
    def handle_broker_message(self, msg_frames):
        """
        Handle broker messages
        """
        pass

    # Sends a message to the broker
    def send_to_broker(self, d):
        """
        Send a message to the broker and log it
        """
        self.req.send_json(d)
        self.log_debug("Sent: %s" % d)

    def handle(self, msg_frames):
        """
        Handle all messages by passing them to the appropriate handlers
        """
        # Unpack the message frames.
        # in the event of a mismatch, format a nice string with msg_frames in
        # the raw, for debug purposes
        assert len(msg_frames) == 3, ((
            "Multipart ZMQ message had wrong length. "
            "Full message contents:\n{}").format(msg_frames))
        assert msg_frames[0] == self.name

        msg = json.loads(msg_frames[2])

        if 'term' in msg and msg['term'] < self.term:
            return

        if not self.all_nodes_started and 'source' in msg:
            self.received_from.add(msg['source'])

        if msg['type'] in ['get', 'getFwd']:
            self.handle_get(msg)

        elif msg['type'] in ['set', 'setFwd']:
            self.handle_set(msg)

        elif msg['type'] == 'hello':
            self.handle_hello(msg)

        elif msg['type'] == 'requestVote':
            self.handle_request_vote(msg)

        elif msg['type'] == 'requestVoteReply':
            self.handle_vote_reply(msg)

        elif msg['type'] == 'appendEntries':
            self.handle_append_entries(msg)

        elif msg['type'] == 'appendEntriesAck':
            self.handle_append_entries_ack(msg)

        else:
            self.log("Received unknown message type: %s" % msg['type'])


# Command-line parameters
@click.command()
@click.option('--pub-endpoint', type=str, default='tcp://127.0.0.1:23310')
@click.option('--router-endpoint', type=str, default='tcp://127.0.0.1:23311')
@click.option('--node-name', type=str)
@click.option('--peer', multiple=True)
@click.option('--debug', is_flag=True)
def run(pub_endpoint, router_endpoint, node_name, peer, debug):

    # Create a node and run it
    n = Node(node_name, pub_endpoint, router_endpoint, peer, debug)

    n.start()


if __name__ == '__main__':
    run()
