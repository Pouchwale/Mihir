"""The visual workflow builder: draw a conversation, validate it, walk it in the simulator.

Deliberately isolated from the live message path - nothing here is reachable from processor.py or
the webhook. A drawn workflow runs only when the dashboard asks it to.
"""
