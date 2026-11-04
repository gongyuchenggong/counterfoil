"""The reconciliation engine.

It only ever sees one input at a time (on_record) and the passage of time
(advance_clock). It never looks at a record before that record's inserted_at,
so the same code runs a replay of CSV files or a live feed.

Order of explanations, strongest first:
    exact match  >  aggregation (several records add up to one)  >  1:1 partial match  >  none
A weaker link (soft or aggregate) always gives way to a stronger explanation.
"""

import heapq
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from . import diagnostics as dx
from .config import Config
from .matcher import (
    closest_to,
    created_gap_seconds,
    find_group,
    mismatches,
    numerics_equal,
    within_tolerance_only,
)
from .models import (
    BREAK,
    CONFIRMATION,
    EXCEPTION_STATUSES,
    INTERNAL,
    OTHER,
    PENDING,
    POSSIBLE_DUPLICATE,
    PROCESSED,
    QUARANTINED,
    SEVERITY_RANK,
    SIDE_LETTER,
    SIDES,
    UNBOOKED,
    UNCONFIRMED,
    BadRow,
    Event,
    Record,
    fmt_ts,
)

FINALIZE = "FINALIZE"  # sentinel step: end of input, fire every remaining deadline


class ManualActionError(ValueError):
    """A manual action that cannot be applied to the current state."""


@dataclass(eq=False)
class RecState:
    rec: Record
    deadline: datetime
    status: str = PENDING
    link: str | None = None  # hard, soft, aggregate, manual (None = unlinked)
    partners: tuple = ()
    link_score: int | None = None  # number of differences behind a soft link
    group_id: str | None = None
    deadline_fired: bool = False
    pair_key: str | None = None  # the case this status belongs to
    first_status_at: datetime | None = None
    severity: str | None = None
    resolved_by: str | None = None
    last_sig: tuple | None = None
    last_what: str = ""
    last_headline: str = ""
    last_event_id: str | None = None
    n_events: int = 0

    @property
    def key(self) -> tuple:
        return (self.rec.side, self.rec.record_id)

    @property
    def rid(self) -> int:
        return self.rec.record_id


class Engine:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.rule_version = cfg.rule_version
        self.clock: datetime | None = None
        self.current_day: date | None = None
        self.heap: list = []
        self._heap_seq = 0
        self.states: dict[tuple, RecState] = {}
        # Pool = records not hard-matched and not resolved by hand; they can still be matched.
        self.pool: dict[str, dict[int, RecState]] = {s: {} for s in SIDES}
        self.coarse: dict[str, dict] = {s: defaultdict(set) for s in SIDES}  # coarse_key -> ids in pool
        self.by_date: dict[str, dict] = {s: defaultdict(set) for s in SIDES}  # trade_date -> ids in pool
        self.matched: dict[str, dict] = {s: defaultdict(set) for s in SIDES}  # coarse_key -> ids already matched
        self.groups: dict[str, list] = {}
        self._group_seq = 0
        self.bad_rows: list[BadRow] = []
        self._events: list[Event] = []
        self.seq = 0
        self._dirty: set = set()
        self._ties: dict[tuple, set] = {}  # record reported as tied -> its candidates, to retry when one goes

    # ================================================================ public API
    def step(self, item) -> list[Event]:
        """Feed one input (Record, BadRow or FINALIZE). Returns the events it caused."""
        if isinstance(item, str) and item == FINALIZE:
            self.finalize()
        elif isinstance(item, BadRow):
            if item.inserted_at is not None:
                self.advance_clock(item.inserted_at)
            self._on_bad_row(item)
        else:
            self.advance_clock(item.inserted_at)
            self.on_record(item)
        return self.drain()

    def drain(self) -> list[Event]:
        events, self._events = self._events, []
        return events

    def advance_clock(self, t: datetime) -> None:
        """Move the engine's clock to t. Deadlines due strictly before t fire first, in order.
        A deadline due exactly at t fires after the record at t is processed."""
        if self.clock is None:
            self.clock, self.current_day = t, t.date()
            return
        while self.heap and self.heap[0][0] < t:
            self._fire_next_deadline()
        self._cross_days(t)
        self.clock = max(self.clock, t)

    def finalize(self) -> None:
        """End of input: fire every remaining deadline and take the last day's snapshot."""
        while self.heap:
            self._fire_next_deadline()
        if self.current_day is not None:
            self._snapshot(self.current_day, self.clock, final=True)

    def on_record(self, rec: Record) -> None:
        key = (rec.side, rec.record_id)
        if key in self.states:
            self._on_repeat_id(self.states[key], rec)
            return
        st = RecState(rec=rec, deadline=rec.inserted_at + self.cfg.deadline)
        self.states[key] = st
        self._pool_add(st)
        self._heap_seq += 1
        heapq.heappush(self.heap, (st.deadline, self._heap_seq, key))
        self._dirty.add(key)
        if not self._try_exact(st):
            for m in self._manual_hits(rec):
                self._notice(m, "NOTE", **dx.manual_hit_message(rec))
        self._data_quality(st)

    def pop_dirty(self) -> list[dict]:
        rows = [self.state_row(self.states[k]) for k in sorted(self._dirty)]
        self._dirty.clear()
        return rows

    def state_row(self, st: RecState) -> dict:
        return {
            "side": st.rec.side,
            "record_id": st.rid,
            "status": st.status,
            "severity": st.severity,
            "link": st.link,
            "partners": list(st.partners),
            "group_id": st.group_id,
            "instrument": st.rec.instrument,
            "trader": st.rec.trader,
            "broker": st.rec.broker,
            "created_at": fmt_ts(st.rec.created_at),
            "inserted_at": fmt_ts(st.rec.inserted_at),
            "deadline": fmt_ts(st.deadline),
            "first_status_at": fmt_ts(st.first_status_at),
            "resolved_by": st.resolved_by,
            "last_event_id": st.last_event_id,
            "what": st.last_what,
        }

    # ================================================================ manual actions
    def apply_manual(self, action: dict) -> list[Event]:
        """Apply an Operations decision. Validates first; raises ManualActionError without side effects."""
        kind = action.get("action")
        actor = action.get("actor") or "ops"
        reason = action.get("reason") or ""
        action_id = action.get("action_id")
        if kind == "accept":
            st = self._get(action["side"], action["record_id"])
            if st.status == PROCESSED:
                raise ManualActionError(f"{st.rec.label} is already PROCESSED")
            if st.status == PENDING:
                raise ManualActionError(
                    f"{st.rec.label} is still inside its {self.cfg.deadline_label} window; nothing to accept yet"
                )
            members = self._link_members(st)
            for m in members:
                prev = m.status
                self._pool_remove(m)
                m.link, m.group_id, m.link_score = "manual", None, None
                self.matched[m.rec.side][m.rec.coarse_key].add(m.rid)
                self._set_status(
                    m,
                    PROCESSED,
                    link="manual",
                    partners=m.partners,
                    pair_key=f"A{action_id}",
                    resolved_by="manual",
                    actor=actor,
                    reason=reason,
                    details={"action_id": action_id, "previous_status": prev},
                    **dx.accepted_message(actor, prev, reason),
                )
            self._retry_ties({m.key for m in members})
        elif kind == "force-match":
            i = self._get(INTERNAL, action["trade_id"])
            c = self._get(CONFIRMATION, action["confirmation_id"])
            for s in (i, c):
                if s.link in ("hard", "manual"):
                    raise ManualActionError(
                        f"{s.rec.label} is already matched with {', '.join(map(str, s.partners)) or 'nothing'}"
                    )
            self._link_hard(i, c, manual=True, actor=actor, reason=reason, action_id=action_id)
        elif kind == "note":
            st = self._get(action["side"], action["record_id"])
            self._notice(
                st,
                "NOTE",
                **dx.note_message(actor, action.get("text", "")),
                details={"action_id": action_id},
                actor=actor,
            )
        else:
            raise ManualActionError(f"unknown action '{kind}'")
        return self.drain()

    def _get(self, side: str, record_id) -> RecState:
        st = self.states.get((side, int(record_id)))
        if st is None:
            raise ManualActionError(f"no {side} record with id {record_id}")
        return st

    # ================================================================ clock
    def _fire_next_deadline(self) -> None:
        due, _, key = heapq.heappop(self.heap)
        self._cross_days(due)
        self.clock = max(self.clock, due)
        st = self.states[key]
        st.deadline_fired = True
        if st.link is None:  # hard/manual are done; soft/aggregate already carry a BREAK
            self._evaluate(st)

    def _cross_days(self, t: datetime) -> None:
        if self.current_day is not None and t.date() > self.current_day:
            boundary = datetime.combine(self.current_day + timedelta(days=1), time.min)
            self._snapshot(self.current_day, boundary, final=False)
            self.current_day = t.date()

    # ================================================================ pool bookkeeping
    def _pool_add(self, st: RecState) -> None:
        r = st.rec
        self.pool[r.side][r.record_id] = st
        self.coarse[r.side][r.coarse_key].add(r.record_id)
        self.by_date[r.side][r.trade_date].add(r.record_id)

    def _pool_remove(self, st: RecState) -> None:
        r = st.rec
        self.pool[r.side].pop(r.record_id, None)
        self.coarse[r.side][r.coarse_key].discard(r.record_id)
        self.by_date[r.side][r.trade_date].discard(r.record_id)

    def _link_members(self, st: RecState) -> list[RecState]:
        if st.link == "aggregate":
            return [self.states[k] for k in self.groups[st.group_id]]
        if st.link == "soft":
            return [st, self.states[(OTHER[st.rec.side], st.partners[0])]]
        return [st]

    def _unlink(self, st: RecState) -> list[RecState]:
        """Break a soft or aggregate link. Returns the other records that lost their link."""
        if st.link not in ("soft", "aggregate"):
            return []
        members = self._link_members(st)
        for m in members:
            m.link, m.partners, m.group_id, m.link_score = None, (), None, None
            self._dirty.add(m.key)
        return [m for m in members if m is not st]

    def _reevaluate(self, st: RecState) -> None:
        """A record lost its link to a better explanation elsewhere."""
        if st.link is not None:
            return
        if st.status == PENDING and not st.deadline_fired:
            return  # still inside its window; its own deadline will decide
        self._evaluate(st)

    # ================================================================ matching
    def _try_exact(self, st: RecState) -> bool:
        r = st.rec
        other = OTHER[r.side]
        cands = [self.pool[other][i] for i in self.coarse[other].get(r.coarse_key, ())]
        cands = [c for c in cands if numerics_equal(r, c.rec, self.cfg)]
        if not cands:
            return False
        best = min(cands, key=closest_to(r))
        self._link_hard(st, best)
        return True

    def _manual_hits(self, r: Record) -> list[RecState]:
        other = OTHER[r.side]
        hits = [self.states[(other, i)] for i in self.matched[other].get(r.coarse_key, ())]
        return [h for h in hits if h.resolved_by == "manual" and numerics_equal(r, h.rec, self.cfg)]

    def _link_hard(
        self, a: RecState, b: RecState, manual: bool = False, actor=None, reason=None, action_id=None
    ) -> None:
        displaced = self._unlink(a) + self._unlink(b)
        i, c = (a, b) if a.rec.side == INTERNAL else (b, a)
        link = "manual" if manual else "hard"
        for s, o in ((i, c), (c, i)):
            s.link, s.partners, s.group_id, s.link_score = link, (o.rid,), None, None
            self._pool_remove(s)
            self.matched[s.rec.side][s.rec.coarse_key].add(s.rid)
        pair_key = f"P-{i.rid}-{c.rid}"
        tolerance_fields = within_tolerance_only(i.rec, c.rec)
        for s, o in ((i, c), (c, i)):
            resolved = s.status if s.status in EXCEPTION_STATUSES else None
            late = int((self.clock - s.deadline).total_seconds() // 60) if resolved else None
            msg = dx.match_message(
                s.rec,
                o.rec,
                self.cfg,
                resolved=resolved,
                late_minutes=late,
                tolerance_fields=tolerance_fields,
                manual_by=actor if manual else None,
                reason=reason,
            )
            self._set_status(
                s,
                PROCESSED,
                link=link,
                partners=(o.rid,),
                pair_key=pair_key,
                resolved_by="manual" if manual else "engine",
                actor=actor,
                reason=reason,
                late_minutes=late,
                details={"action_id": action_id} if manual else None,
                **msg,
            )
        for d in displaced:
            if d not in (i, c):
                self._reevaluate(d)
        self._retry_ties({i.key, c.key})

    def _retry_ties(self, gone: set) -> None:
        """These records left the pool, so a record tied between them may now have one answer, or none."""
        for key in sorted(k for k, candidates in self._ties.items() if candidates & gone):
            st = self.states[key]
            del self._ties[key]
            if st.link is None and st.status == BREAK:
                self._evaluate(st)

    def _evaluate(self, st: RecState) -> None:
        """Deadline logic: the strongest available explanation wins. An exact match cannot be
        waiting here: on_record links one as soon as both sides are in the pool."""
        if self.cfg.rules.aggregation.enabled and self._try_aggregate(st):
            return
        if self._try_partial(st):
            return
        self._assign_unmatched(st)

    # ---- aggregation
    def _agg_eligible(self, side: str, coarse_key: tuple) -> list[RecState]:
        return [
            self.pool[side][i] for i in self.coarse[side].get(coarse_key, ()) if self.pool[side][i].link != "aggregate"
        ]

    def _try_aggregate(self, st: RecState) -> bool:
        r = st.rec
        other = OTHER[r.side]
        # Case A: st is the single record; several on the other side add up to it.
        parts = [c for c in self._agg_eligible(other, r.coarse_key) if c.rec.volume < r.volume]
        if len(parts) >= 2:
            group, alts = find_group(r, parts, self.cfg)
            if group:
                self._link_aggregate(st, group, alts)
                return True
        # Case B: st is one of several that add up to a single record on the other side.
        singles = sorted(
            (c for c in self._agg_eligible(other, r.coarse_key) if c.rec.volume > r.volume), key=closest_to(r)
        )
        mine = self._agg_eligible(r.side, r.coarse_key)
        for single in singles:
            pool = [m for m in mine if m.rec.volume < single.rec.volume]
            group, alts = find_group(single.rec, pool, self.cfg, must_include=st)
            if group:
                self._link_aggregate(single, group, alts)
                return True
        return False

    def _link_aggregate(self, single: RecState, parts: list[RecState], alts: int) -> None:
        members = [single] + parts
        displaced = []
        for m in members:
            displaced += self._unlink(m)
        self._group_seq += 1
        gid = f"G{self._group_seq}"
        self.groups[gid] = [m.key for m in members]
        single.link, single.partners, single.group_id = "aggregate", tuple(p.rid for p in parts), gid
        for p in parts:
            p.link, p.partners, p.group_id = "aggregate", (single.rid,), gid
        msg = dx.aggregate_message(single.rec, [p.rec for p in parts], alts)
        for m in members:
            self._set_status(m, BREAK, link="aggregate", partners=m.partners, pair_key=gid, **msg)
        for d in displaced:
            if d not in members:
                self._reevaluate(d)

    # ---- 1:1 partial match
    def _partial_candidates(self, st: RecState) -> list:
        r = st.rec
        other = OTHER[r.side]
        out = []
        window = self.cfg.rules.partial_match.date_window_days
        for offset in range(-window, window + 1):
            day = r.trade_date + timedelta(days=offset)
            for cid in self.by_date[other].get(day, ()):
                c = self.pool[other][cid]
                if c.link == "aggregate":
                    continue
                fields = mismatches(r, c.rec, self.cfg)
                n = len(fields)
                if n == 0 or n > self.cfg.rules.partial_match.max_mismatches:
                    continue
                # A soft link only gives way to a strictly better 1:1 explanation.
                if c.link == "soft" and c.partners != (r.record_id,) and n >= c.link_score:
                    continue
                out.append((n, created_gap_seconds(r, c.rec), c.rec.inserted_at, c.rid, c, fields))
        out.sort(key=lambda x: x[:4])
        return out

    def _best_for(self, c: RecState) -> tuple:
        cands = self._partial_candidates(c)
        if not cands:
            return None, set()
        best = cands[0][0]
        return best, {x[3] for x in cands if x[0] == best}

    def _try_partial(self, st: RecState) -> bool:
        cands = self._partial_candidates(st)
        if not cands:
            return False
        for n in sorted({x[0] for x in cands}):
            available = []
            for x in (x for x in cands if x[0] == n):
                best, ids = self._best_for(x[4])
                if best == n and st.rid in ids:  # st is among the candidate's best options
                    available.append((x, len(ids) == 1))
            if not available:
                continue  # every candidate at this level prefers someone else
            if len(available) == 1 and available[0][1]:
                x = available[0][0]
                self._link_soft(st, x[4], x[5])
            else:
                self._set_ambiguous(st, [(x[4], x[5]) for x, _ in available])
            return True
        return False

    def _link_soft(self, st: RecState, c: RecState, fields: list) -> None:
        displaced = self._unlink(c)  # c may give up a weaker soft link
        st.link = c.link = "soft"
        st.partners, c.partners = (c.rid,), (st.rid,)
        st.link_score = c.link_score = len(fields)
        i, k = (st, c) if st.rec.side == INTERNAL else (c, st)
        msg = dx.break_message(i.rec, k.rec, fields, self.cfg)
        pair_key = f"B-{i.rid}-{k.rid}"
        for s, o in ((i, k), (k, i)):
            self._set_status(s, BREAK, link="soft", partners=(o.rid,), pair_key=pair_key, **msg)
        for d in displaced:
            if d is not st:
                self._reevaluate(d)

    def _set_ambiguous(self, st: RecState, options: list) -> None:
        msg = dx.ambiguous_message(st.rec, [(c.rec, f) for c, f in options], self.cfg)
        self._ties[st.key] = {c.key for c, _ in options}
        self._set_status(st, BREAK, link="ambiguous", partners=tuple(c.rid for c, _ in options), **msg)

    # ---- nothing fits
    def _identical_matched(self, r: Record):
        same = [self.states[(r.side, i)] for i in self.matched[r.side].get(r.coarse_key, ()) if i != r.record_id]
        same = [s for s in same if numerics_equal(r, s.rec, self.cfg)]
        if not same:
            return None, None
        dup = min(same, key=closest_to(r))
        partner = self.states.get((OTHER[r.side], dup.partners[0])) if dup.partners else None
        return dup, partner

    def _assign_unmatched(self, st: RecState) -> None:
        r = st.rec
        dup, partner = self._identical_matched(r)
        hits = self._manual_hits(r)
        manual_hit = hits[0].rec if hits else None
        details = {}
        if r.side == INTERNAL:
            twin, twin_partner = dup.rec if dup else None, partner.rec if partner else None
            if dup is not None and created_gap_seconds(r, twin) <= self.cfg.duplicate_window.total_seconds():
                msg, status = dx.duplicate_message(r, twin, twin_partner), POSSIBLE_DUPLICATE
            else:
                msg, status = dx.unconfirmed_message(r, self.cfg, manual_hit, twin, twin_partner), UNCONFIRMED
            if dup is not None:
                details = {"duplicate_of": dup.rid}
        else:
            msg = dx.unbooked_message(
                r, self.cfg, dup.rec if dup else None, partner.rec if partner else None, manual_hit
            )
            status = UNBOOKED
            if dup is not None:
                details = {"identical_confirmation": dup.rid}
        self._set_status(st, status, details=details or None, **msg)

    # ================================================================ data quality
    def _data_quality(self, st: RecState) -> None:
        r = st.rec
        ahead = r.created_at - r.inserted_at
        if ahead > timedelta(0):
            partner = self.states[(OTHER[r.side], st.partners[0])].rec if st.link == "hard" else None
            msg = (
                dx.clock_skew_message(r, self.cfg, partner)
                if ahead <= self.cfg.clock_skew_tolerance
                else dx.future_dated_message(r)
            )
            self._notice(st, "DATA_QUALITY", **msg)

    def _on_repeat_id(self, existing: RecState, rec: Record) -> None:
        if existing.rec.fingerprint == rec.fingerprint:
            self._notice(existing, "DATA_QUALITY", **dx.resend_message(rec))
            return
        self._notice(
            existing,
            "QUARANTINE",
            **dx.conflict_message(existing.rec, rec),
            details={"raw": rec.raw, "source_row": rec.source_row, "conflicting_id": rec.record_id},
        )

    def _on_bad_row(self, row: BadRow) -> None:
        self.bad_rows.append(row)
        self._emit(
            Event(
                event_id=f"Q-{SIDE_LETTER[row.side]}-row{row.source_row}",
                seq=0,
                sim_time=fmt_ts(self.clock),
                kind="QUARANTINE",
                side=row.side,
                status=QUARANTINED,
                **dx.bad_row_message(row),
                details={
                    "raw": row.raw,
                    "source_row": row.source_row,
                    "record_id_text": row.record_id_text,
                    "reasons": list(row.reasons),
                },
                rule_version=self.rule_version,
            )
        )

    # ================================================================ events
    def _set_status(
        self,
        st: RecState,
        status: str,
        *,
        link=None,
        partners=(),
        severity=None,
        what="",
        why="",
        next="",
        note="",
        mismatches=(),
        pair_key=None,
        resolved_by="engine",
        actor=None,
        reason=None,
        details=None,
        late_minutes=None,
        headline="",
    ) -> None:
        sig = (status, link, tuple(partners), severity, what, note)
        if st.last_sig == sig:
            return  # nothing new to say
        prev = st.status
        st.status, st.severity, st.last_sig, st.last_what, st.pair_key = status, severity, sig, what, pair_key
        st.last_headline = headline or what
        details = dict(details or {}, headline=st.last_headline)
        st.resolved_by = resolved_by if status == PROCESSED else None
        if st.first_status_at is None:
            st.first_status_at = self.clock
        self._dirty.add(st.key)
        self._emit_for(
            st,
            kind="STATUS",
            status=status,
            prev_status=prev,
            link=link,
            counterparty_ids=list(partners),
            severity=severity,
            mismatches=list(mismatches),
            what=what,
            why=why,
            next=next,
            note=note,
            pair_key=pair_key,
            resolved_by=st.resolved_by,
            actor=actor,
            reason=reason,
            details=details,
            late_minutes=late_minutes,
        )

    def _notice(
        self, st: RecState, kind: str, severity: str, what: str, why: str, next: str, details=None, actor=None
    ) -> None:
        self._emit_for(
            st,
            kind=kind,
            status=None,
            severity=severity,
            what=what,
            why=why,
            next=next,
            details=details or {},
            actor=actor,
        )

    def _emit_for(self, st: RecState, **fields) -> None:
        st.n_events += 1
        event_id = f"{SIDE_LETTER[st.rec.side]}-{st.rid}-{st.n_events}"
        st.last_event_id = event_id
        self._dirty.add(st.key)
        self._emit(
            Event(
                event_id=event_id,
                seq=0,
                sim_time=fmt_ts(self.clock),
                side=st.rec.side,
                record_id=st.rid,
                record=st.rec.summary(),
                rule_version=self.rule_version,
                **fields,
            )
        )

    def _emit(self, event: Event) -> None:
        self.seq += 1
        event.seq = self.seq
        self._events.append(event)

    def _snapshot(self, day: date, as_of: datetime, final: bool) -> None:
        counts = {side: Counter() for side in SIDES}
        open_items = []
        for st in self.states.values():
            counts[st.rec.side][st.status] += 1
            if st.status in EXCEPTION_STATUSES:
                open_items.append(
                    {
                        "side": st.rec.side,
                        "record_id": st.rid,
                        "status": st.status,
                        "severity": st.severity,
                        "age_minutes": int((as_of - st.rec.inserted_at).total_seconds() // 60),
                        "counterparty_ids": list(st.partners),
                        "what": st.last_what,
                        "headline": st.last_headline,
                        "pair_key": st.pair_key,
                    }
                )
        open_items.sort(key=lambda x: (-SEVERITY_RANK[x["severity"]], -x["age_minutes"], x["side"], x["record_id"]))
        self._emit(
            Event(
                event_id=f"S-{day.isoformat()}",
                seq=0,
                sim_time=fmt_ts(as_of),
                kind="SNAPSHOT",
                what=f"End of day {day.isoformat()}" + (" (end of data)" if final else ""),
                details={
                    "day": day.isoformat(),
                    "final": final,
                    "quarantined_rows": len(self.bad_rows),
                    "counts": {s: dict(c) for s, c in counts.items()},
                    "open": open_items,
                },
                rule_version=self.rule_version,
            )
        )
