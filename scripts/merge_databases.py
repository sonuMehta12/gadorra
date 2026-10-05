"""Copy registrations from one database into another without overwriting anything.

The target is the app's own database (DATABASE_URL). The source is given as
SOURCE_DATABASE_URL. Rows are only ever added to the target: nothing there is
updated or deleted.

    # 1. dry run -- reports what would happen, writes nothing
    SOURCE_DATABASE_URL='postgresql://...' python scripts/merge_databases.py

    # 2. do it
    SOURCE_DATABASE_URL='postgresql://...' python scripts/merge_databases.py --apply

How rows are matched:
  districts      by name, never by id -- the two databases may have seeded
                 them in a different order
  students       by mobile. A student the target already has is reused and left
                 as it is; their registrations still come across.
  registrations  one per student per phase. If the target already has one for
                 that student and phase, the source's is skipped and reported --
                 a person decides which is right, not this script.
  acknowledgements, payments, notifications
                 follow their registration. Rows the target already has (same
                 id, order id, payment id or acknowledgement number) are skipped.

Not copied: registrations still PENDING_PAYMENT (they may yet be paid in the
source; run again once they settle), otp_requests (dead after ten minutes),
revoked_tokens (logout state for expired tokens), webhook_events (Razorpay's
delivery log).

Refuses to --apply when a student PAID in the source but has an unpaid
registration in the target: skipping it would lose their payment.

Everything runs in one transaction. If any step fails, or a copied registration
would lose its acknowledgement number, nothing is written. Running it again after
a successful --apply adds nothing: every row is found already present.
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import create_engine, inspect, select  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402

import app.models  # noqa: E402,F401  -- registers the tables
from app.config import Settings, settings  # noqa: E402
from app.database import Base  # noqa: E402

T = Base.metadata.tables


class Abort(Exception):
    pass


def common_columns(src_insp, tgt_insp, table: str) -> list[str]:
    """Only columns both databases have, in case one runs slightly older code."""
    src = {c["name"] for c in src_insp.get_columns(table)}
    tgt = {c["name"] for c in tgt_insp.get_columns(table)}
    return [c.name for c in T[table].columns if c.name in src and c.name in tgt]


def fetch(conn, table: str, cols: list[str], where=None) -> list[dict]:
    t = T[table]
    q = select(*[t.c[c] for c in cols])
    if where is not None:
        q = q.where(where)
    return [dict(r._mapping) for r in conn.execute(q)]


def main(apply: bool) -> None:
    source_url = os.environ.get("SOURCE_DATABASE_URL", "").strip()
    if not source_url:
        raise SystemExit("set SOURCE_DATABASE_URL to the database to copy FROM")

    source_url = Settings(_env_file=None, database_url=source_url).database_url
    target_url = settings.database_url
    s, t = make_url(source_url), make_url(target_url)
    if (s.host, s.port, s.database) == (t.host, t.port, t.database):
        raise SystemExit("source and target are the same database -- refusing")

    print(f"FROM  {s.host}/{s.database}")
    print(f"INTO  {t.host}/{t.database}")
    print(f"MODE  {'APPLY -- will write' if apply else 'DRY RUN -- writes nothing'}\n")

    src_engine = create_engine(source_url)
    tgt_engine = create_engine(target_url)
    src_insp, tgt_insp = inspect(src_engine), inspect(tgt_engine)

    tables = ["districts", "students", "registrations", "acknowledgements", "payments", "notifications"]
    for name in tables:
        for label, insp in (("source", src_insp), ("target", tgt_insp)):
            if not insp.has_table(name):
                raise SystemExit(f"{label} database has no '{name}' table")
    cols = {name: common_columns(src_insp, tgt_insp, name) for name in tables}

    added = {k: 0 for k in ("students", "registrations", "acknowledgements", "payments", "notifications")}
    reused_students, already, conflicts = [], {k: 0 for k in added}, []

    with src_engine.connect() as src, tgt_engine.connect() as tgt:
        trans = tgt.begin()
        try:
            # ---- districts: map source id -> target id by name
            tgt_district_by_name = {r["name"]: r["id"] for r in fetch(tgt, "districts", ["id", "name"])}
            district_map = {}
            for r in fetch(src, "districts", ["id", "name"]):
                if r["name"] not in tgt_district_by_name:
                    raise Abort(f"district '{r['name']}' exists in the source but not the target")
                district_map[r["id"]] = tgt_district_by_name[r["name"]]

            # ---- students: by mobile
            tgt_students = {r["mobile"]: r["id"] for r in fetch(tgt, "students", ["id", "mobile"])}
            student_map = {}
            for row in fetch(src, "students", cols["students"]):
                if row["mobile"] in tgt_students:
                    student_map[row["id"]] = tgt_students[row["mobile"]]
                    if tgt_students[row["mobile"]] == row["id"]:
                        already["students"] += 1
                    else:
                        reused_students.append(row["mobile"])
                    continue
                row["district_id"] = district_map[row["district_id"]]
                tgt.execute(T["students"].insert().values(**row))
                student_map[row["id"]] = row["id"]
                added["students"] += 1

            # ---- registrations: one per student per phase
            tgt_regs = fetch(tgt, "registrations", ["id", "student_id", "phase", "status"])
            tgt_reg_ids = {r["id"] for r in tgt_regs}
            tgt_reg_slot = {(r["student_id"], r["phase"]): r for r in tgt_regs}
            src_students = {r["id"]: r["mobile"] for r in fetch(src, "students", ["id", "mobile"])}

            copied_regs = set()
            paid_regs = set()
            # The source paid, the target did not: skipping the source here would
            # lose a student's payment and acknowledgement. Never decided silently.
            paid_left_behind = []
            # Still open in the source: the student may yet pay there. Copied now,
            # it would stay PENDING in the target forever (this script never updates)
            # and the target's sync job could settle the same order a second time.
            # Left for a later run, when it has become PAID or EXPIRED.
            pending_left = 0
            for row in fetch(src, "registrations", cols["registrations"]):
                if row["id"] in tgt_reg_ids:
                    already["registrations"] += 1
                    continue
                if row["status"].value == "PENDING_PAYMENT":
                    pending_left += 1
                    continue
                student_id = student_map[row["student_id"]]
                slot = tgt_reg_slot.get((student_id, row["phase"]))
                if slot is not None:
                    src_status = row["status"].value
                    tgt_status = slot["status"].value if hasattr(slot["status"], "value") else str(slot["status"])
                    if src_status == "PAID" and tgt_status != "PAID":
                        paid_left_behind.append(
                            f"mobile {src_students[row['student_id']]}: PAID in the source, "
                            f"{tgt_status} in the target"
                        )
                        continue
                    conflicts.append(
                        f"mobile {src_students[row['student_id']]}: {row['phase'].value} registration "
                        f"exists in both (source {row['status'].value}, target {slot['status'].value}) "
                        f"-- kept the target's"
                    )
                    continue
                row["student_id"] = student_id
                tgt.execute(T["registrations"].insert().values(**row))
                tgt_reg_slot[(student_id, row["phase"])] = row
                copied_regs.add(row["id"])
                if row["status"].value == "PAID":
                    paid_regs.add(row["id"])
                added["registrations"] += 1

            # ---- acknowledgements: follow their registration
            tgt_ack_ids = {r["id"] for r in fetch(tgt, "acknowledgements", ["id"])}
            tgt_ack_numbers = {r["number"] for r in fetch(tgt, "acknowledgements", ["number"])}
            acked = set()
            for row in fetch(src, "acknowledgements", cols["acknowledgements"]):
                if row["id"] in tgt_ack_ids:
                    already["acknowledgements"] += 1
                    continue
                if row["registration_id"] not in copied_regs:
                    continue
                if row["number"] in tgt_ack_numbers:
                    raise Abort(
                        f"acknowledgement {row['number']} is already used by another student in the "
                        f"target. Nothing was written. This needs a manual decision."
                    )
                if row.get("redeemed_by_registration_id") not in copied_regs:
                    row["redeemed_by_registration_id"] = None
                tgt.execute(T["acknowledgements"].insert().values(**row))
                tgt_ack_numbers.add(row["number"])
                acked.add(row["registration_id"])
                added["acknowledgements"] += 1

            # A collision already aborted above, so anything left here was missing in
            # the source too. Worth knowing, not worth blocking the rest.
            missing_ack = sorted(str(r) for r in paid_regs - acked)

            if paid_left_behind and apply:
                raise Abort(
                    f"{len(paid_left_behind)} student(s) paid in the source but have an unpaid "
                    f"registration in the target. Copying would leave their payment behind, so "
                    f"nothing was written. Resolve these first:\n    "
                    + "\n    ".join(paid_left_behind)
                )

            # ---- payments
            tgt_pay = fetch(tgt, "payments", ["id", "razorpay_order_id", "razorpay_payment_id"])
            tgt_pay_ids = {r["id"] for r in tgt_pay}
            tgt_orders = {r["razorpay_order_id"] for r in tgt_pay}
            tgt_pids = {r["razorpay_payment_id"] for r in tgt_pay if r["razorpay_payment_id"]}
            for row in fetch(src, "payments", cols["payments"]):
                if row["id"] in tgt_pay_ids:
                    already["payments"] += 1
                    continue
                if row["registration_id"] not in copied_regs:
                    continue
                if row["razorpay_order_id"] in tgt_orders or (
                    row.get("razorpay_payment_id") and row["razorpay_payment_id"] in tgt_pids
                ):
                    conflicts.append(f"payment {row['razorpay_order_id']} already in the target -- skipped")
                    continue
                tgt.execute(T["payments"].insert().values(**row))
                added["payments"] += 1

            # ---- notifications: the WhatsApp delivery log, kept for the record
            tgt_note_ids = {r["id"] for r in fetch(tgt, "notifications", ["id"])}
            for row in fetch(src, "notifications", cols["notifications"]):
                if row["id"] in tgt_note_ids:
                    already["notifications"] += 1
                    continue
                if row.get("registration_id") not in copied_regs:
                    continue
                tgt.execute(T["notifications"].insert().values(**row))
                added["notifications"] += 1

        except Abort as exc:
            trans.rollback()
            print(f"STOPPED: {exc}")
            raise SystemExit(1)
        except Exception:
            trans.rollback()
            raise

        # ---- report
        print(f"{'':18}{'would add' if not apply else 'added':>10}{'already there':>15}")
        for k in added:
            print(f"{k:18}{added[k]:>10}{already[k]:>15}")
        if reused_students:
            print(f"\n{len(reused_students)} student(s) already in the target by mobile, reused as they are:")
            for m in reused_students:
                print(f"  {m}")
        if pending_left:
            print(f"\n{pending_left} registration(s) still PENDING_PAYMENT in the source were left there.")
            print("   Run this again once they are paid or expired to bring them across.")
        if paid_left_behind:
            print(f"\n!! {len(paid_left_behind)} student(s) PAID in the source but NOT in the target.")
            print("   --apply will refuse to run until these are resolved:")
            for m in paid_left_behind:
                print(f"     {m}")
        if missing_ack:
            print(f"\n{len(missing_ack)} paid registration(s) have no acknowledgement number in the source either:")
            for r in missing_ack:
                print(f"  registration {r}")
        if conflicts:
            print(f"\n{len(conflicts)} conflict(s), skipped -- the target's record was kept:")
            for c in conflicts:
                print(f"  {c}")

        if apply:
            trans.commit()
            print("\nCommitted.")
        else:
            trans.rollback()
            print("\nDry run: nothing was written. Run again with --apply to do it.")


if __name__ == "__main__":
    main(apply="--apply" in sys.argv[1:])
