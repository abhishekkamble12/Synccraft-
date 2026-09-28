Real-Time Collaborative Sync Engine
The backend behind Google Docs-style live editing — built from first principles.

WHY THIS ISN'T A GENERIC PROJECT
Most developers consume sync engines (Firebase, Liveblocks) without understanding how concurrent edits are
merged without conflict. Implementing the conflict-resolution logic yourself is one of the most respected,
least-attempted backend skills there is.

CORE REQUIREMENTS
01 Multiple clients can edit the SAME shared document concurrently and converge to an identical final state.
02 Implement a conflict resolution strategy yourself — either Operational Transformation or a CRDT (no pulling in a
pre-built CRDT library).
03 Edits made offline must merge correctly once a client reconnects, without overwriting others' changes.
04 Maintain full edit history and support reverting the document to any earlier point in time.
05 Broadcast live changes to all connected clients with sub-second propagation.
06 Prove convergence formally: write a test that applies the same set of edits in different orders across simulated
clients and asserts identical final state.
07 Handle a client disconnect/reconnect mid-edit without data loss or duplication.

STRETCH GOALS SKILLS YOU'LL MASTER
+ Extend beyond plain text to a structured document
(e.g. nested lists or a simple spreadsheet grid).
+ Add presence indicators (who's online, where their
cursor is) as a secondary real-time channel.

CRDTs/OT, real-time protocols, distributed consistency,