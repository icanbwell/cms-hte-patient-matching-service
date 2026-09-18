"""Manual-testing UI: IAL2 token decoding and pairwise patient matching.

Gated behind ENABLE_TESTING_UI (see api.py) -- disabled by default because
its endpoints decode arbitrary IAL2 tokens and run matching on arbitrary
pasted FHIR Patients with no auth of their own.
"""
