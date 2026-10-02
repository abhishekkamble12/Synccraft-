"""
Rebasing unacknowledged local ops onto a fresh server state.

A client that was offline for longer than the server's session TTL may hold ops
whose parent or delete target has since been garbage-collected (tombstones are
dropped once every *live* session has moved past them). The server rejects such
ops as `stale`; the client then reloads the current state and replays its edits
with this function instead of losing them.

Every pending op is re-issued with fresh ids, in order:

* an insert keeps its parent if that still exists; otherwise it is re-anchored to
  the nearest character to the left of its original position (in the client's
  old local order) that still exists. Later pending ops that referenced the old
  ids are remapped to the new ones;
* a delete keeps only the targets that still exist (the others were tombstones
  already) and is dropped if none remain.

Re-issuing everything, rather than resending the ops that happen to still apply,
keeps each site's ids strictly increasing on the server, which `RGA.validate`
enforces.

The rebased ops are applied to `fresh` and returned for sending.
"""

from crdt.ids import ROOT, CharId
from crdt.ops import Op
from crdt.rga import RGA


def rebase_pending(old: RGA | None, fresh: RGA, pending: list[Op]) -> list[Op]:
    remap: dict[CharId, CharId] = {}
    out: list[Op] = []
    committed = fresh.version_vector  # before we add ids of our own
    if old is not None:
        fresh.clock.update(old.clock.value)

    def resolve(cid: CharId) -> CharId | None:
        cid = remap.get(cid, cid)
        return cid if fresh.has(cid) else None

    for op in pending:
        if op.type == "insert":
            assert op.char_id is not None and op.parent_id is not None and op.text is not None
            # A site's ops commit in order with increasing ids, so if the server has
            # seen an id this high from the site, this op was committed (its ack was
            # lost) even if its characters were deleted and collected since.
            if op.char_id.clock <= committed.get(op.char_id.site_id, 0):
                continue
            parent = resolve(op.parent_id)
            if parent is None:
                parent = _surviving_left_neighbour(old, fresh, remap, op.parent_id)
            new_op = fresh.local_insert_after(parent, op.text)
            for old_id, new_id in zip(op.inserted_ids(), new_op.inserted_ids(), strict=True):
                remap[old_id] = new_id
            out.append(new_op)
        else:
            alive = [cid for cid in map(resolve, op.target_ids()) if cid is not None]
            new_delete = fresh.local_delete_ids(alive)
            if new_delete is not None:
                out.append(new_delete)
    return out


def _surviving_left_neighbour(
    old: RGA | None, fresh: RGA, remap: dict[CharId, CharId], missing: CharId
) -> CharId:
    """Walk left from `missing` in the old local order to the first id `fresh` still has."""
    if old is not None and old.has(missing):
        node = old._nodes[missing]
        while node is not old._head:
            cid = remap.get(node.char_id, node.char_id)
            if fresh.has(cid):
                return cid
            assert node.prev is not None
            node = node.prev
        return ROOT
    # No local history to consult (e.g. the page was reloaded): append at the end.
    length = fresh.visible_len()
    return fresh.char_id_at(length - 1) if length else ROOT
