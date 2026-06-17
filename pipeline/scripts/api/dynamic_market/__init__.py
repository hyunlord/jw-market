"""Runtime dynamic-market API building blocks.

The package keeps dynamic market computation in three independently reusable
pieces:

* resolver: turns view-specific filters into a deterministic brand set;
* aggregator: computes metrics from a brand set without knowing the view;
* composer: serializes computed metrics into the public response schema.
"""
