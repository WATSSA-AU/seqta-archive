#!/usr/bin/env python3
"""
Generate a small FAKE SEQTA offboarding export for tests and demos.

Every name, address, phone number and note is invented. Column layouts match a real
SEQTA export (headers only, from tests/fixtures/seqta_schema.json).

    python tests/make_fake_export.py OUT_DIR [--drop-table sickbay] [--drop-column contact.phone_work]
"""

import argparse
import csv
import json
import os
import random
from datetime import date, datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
SCHEMA = json.load(open(os.path.join(HERE, "fixtures", "seqta_schema.json")))

# Files SEQTA splits by year; the schema lists them under a wildcard name
CMSG_COLS = ["id", "in_reply_to", "subject", "contents", "sender_name", "sender", "blind",
             "updated_date"]
CORR_COLS = ["id", "type", "contents", "subject", "updated_date", "cmessage"]
PART_COLS = ["id", "log_correspondence", "address", "person_id", "person_type", "role",
             "success", "updated_date"]

FIRST = ["Ava", "Noah", "Mia", "Leo", "Isla", "Kai", "Zara", "Eli", "Ruby", "Finn", "Maya",
         "Jack", "Ivy", "Theo", "Lily", "Max", "Chloe", "Sam", "Ella", "Ben"]
LAST = ["Testwood", "Sample", "Example", "Placeholder", "Fakeley", "Mockford", "Dummett",
        "Specimen", "Trialson", "Notreal"]
LONG_NOTE = ("This is a deliberately long invented pastoral note used to check that the archive "
             "never truncates text. " * 12).strip()


def ts(d, h=9, m=0):
    return datetime(d.year, d.month, d.day, h, m).strftime("%Y-%m-%d %H:%M:%S")


def stamp(d):
    return datetime(d.year, d.month, d.day, 10, 0).strftime("%Y-%m-%d %H:%M:%S.000000+08")


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("out")
    ap.add_argument("--school", default="au_wa_testschool")
    ap.add_argument("--students", type=int, default=24)
    ap.add_argument("--drop-table", action="append", default=[])
    ap.add_argument("--drop-column", action="append", default=[], help="table.column")
    a = ap.parse_args(argv)
    rnd = random.Random(42)

    root = os.path.join(a.out, a.school)
    data = os.path.join(root, "data")
    files = os.path.join(root, "files")
    os.makedirs(data, exist_ok=True)
    os.makedirs(files, exist_ok=True)

    rows = {name: [] for name in SCHEMA}

    def add(table, **kw):
        rows[table + ".csv"].append(kw)

    # ---- lookups
    for i, (code, name) in enumerate([("NOR", "North"), ("SOU", "South"), ("EAS", "East")], 1):
        add("house", id=str(i), code=code, name=name, description=name)
    for i, y in enumerate(["07", "08", "09", "10", "11", "12"], 1):
        add("schoolyear", id=str(i), code=y, name=y)
    for i in range(1, 7):
        add("rollgroup", id=str(i), code=f"{i + 6}A1", name=f"{i + 6}A1")
    add("country", id="1", code_alpha2="AU", name="Australia")
    add("language", id="1", name="English")
    for i, lbl in enumerate(["Academic", "Behaviour Concern", "Pastoral Care", "Attendance",
                             "Medical", "Parent Contact Record"], 1):
        add("pastoralcaretype", id=str(i), label=lbl)
    for i, t in enumerate(["custody", "doctor", "emergency", "general", "medical"], 1):
        add("studentnotetype", id=str(i), type=t)
    add("studentplantype", id="1", label="Individual education plan")
    add("contacttype", id="1", label="Mother")
    add("contacttype", id="2", label="Father")

    # ---- staff
    staff = []
    for i in range(1, 7):
        code = str(20000 + i)
        sal = f"Ms Teacher{i} Example"
        staff.append((str(i), code, sal))
        add("staff", staffID=str(i), staffCode=code, salutation=sal, firstname=f"Teacher{i}",
            surname="Example", email=f"teacher{i}@example.invalid")

    # ---- students, contacts, records
    pc_id = sb_id = note_id = plan_id = goal_id = gh_id = contact_id = 0
    msg_id, corr_id, part_id, msgp_id = 100, 1000, 1, 1
    base = date(2024, 2, 1)
    corr_rows, part_rows, msg_rows = [], [], []
    for n in range(1, a.students + 1):
        sid, code = str(n), str(9000 + n)
        first, last = FIRST[(n - 1) % len(FIRST)], LAST[(n - 1) % len(LAST)]
        status = "FULL" if n % 3 else "LEFT"
        add("student", studentID=sid, studentCode=code, firstname=first, prefname=first,
            surname=last, username=f"{first}.{last}".lower(),
            birth_date=str(date(2010, 1, 1) + timedelta(days=n * 40)), gender=rnd.choice("mf"),
            entry_date=str(base - timedelta(days=365)), exit_date="" if status == "FULL" else
            str(base + timedelta(days=200)), rollgroup=str((n % 6) + 1),
            schoolyear=str((n % 6) + 1), house=str((n % 3) + 1), status=status,
            email=f"{first}.{last}@student.example.invalid".lower(), country_birth="1",
            language_home="1", alert_medical="t" if n % 5 == 0 else "f",
            notes_medical="Invented allergy note." if n % 5 == 0 else "",
            government_id=str(30000000 + n), updated_date=stamp(base))
        for rel, typ in (("Mother", "1"), ("Father", "2")):
            contact_id += 1
            add("contact", contactID=str(contact_id), contactCode=str(50000 + contact_id),
                salutation=f"{'Ms' if rel == 'Mother' else 'Mr'} Parent{contact_id} {last}",
                firstname=f"Parent{contact_id}", surname=last,
                phone_mobile=f"0400 000 {contact_id:03d}", email=f"p{contact_id}@example.invalid",
                address_street=f"{n} Example Street", address_suburb="TESTVILLE",
                address_state="WA", address_postcode="6999")
            add("studentContact", studentID=sid, studentCode=code, contactID=str(contact_id),
                contactcode=str(50000 + contact_id), contacttypeID=typ, contacttypelabel=rel,
                priority="1" if rel == "Mother" else "2", email="t", sms="t", mail="f")
        for k in range(rnd.randint(1, 6) if n > 1 else 4):
            pc_id += 1
            st = staff[rnd.randrange(len(staff))]
            d = base + timedelta(days=rnd.randint(0, 600))
            details = LONG_NOTE if (n == 1 and k == 0) else \
                f"Invented pastoral entry {pc_id} for {first}. Followed up with family."
            add("pastoralcare", id=str(pc_id), staffID=st[0], staffCode=st[1], studentID=sid,
                studentCode=code, **{"from": ts(d, 9 + k % 6), "until": ""}, details=details,
                requires_followup="t" if k == 1 else "f", followed_up="f",
                student_present="t" if k == 2 else "f", points="0", tag="Phone" if k == 3 else "",
                emailed_parents="t" if k % 2 else "f", emailed_individuals="[]",
                pastoralcaretype=str(1 + k % 6), restricted="f", updated_date=stamp(d))
            if k == 0:
                folder = os.path.join(files, "pastoralcare", str(d.year))
                os.makedirs(folder, exist_ok=True)
                with open(os.path.join(folder, f"pc-{pc_id}_f-{pc_id}_st-{code}_note.pdf"), "wb") as f:
                    f.write(b"%PDF-1.4\n% fake attachment\n")
        if n % 4 == 0:
            sb_id += 1
            add("sickbay", id=str(sb_id), staffID="1", staffCode=staff[0][1], studentID=sid,
                studentCode=code, **{"from": ts(base, 11), "until": ts(base, 12)},
                location="school nurse", notes="Invented headache.", treatment_summary="Rest",
                ambulance="f", parentcontact="t", parentcontact_details="Called mother",
                discharged="f", updated_date=stamp(base))
            add("sickbay_treatment", id=str(sb_id), sickbay=str(sb_id), timestamp=ts(base, 11),
                treatment_01="water", treatment_02="rest")
        note_id += 1
        add("studentnote", studentCode=code, studentSurname=last, studentFirstname=first,
            typeID="5", type="medical", id=str(note_id), note="Invented medical note.",
            internal="t", updated_by_staffCode=staff[0][1], updated_date=stamp(base))
        if n % 5 == 0:
            add("studentalert", studentID=sid, studentCode=code, typeID="1", alertType="medical",
                updated_date=stamp(base))
        if n % 3 == 0:
            plan_id += 1
            add("studentplan", id=str(plan_id), studentID=sid, studentCode=code,
                studentplantypeID="1", studentplantype="Individual education plan",
                status="active", background="Invented plan background.", panel="Ms Example",
                review_date="2024-06-01", updated_date=stamp(base))
            folder = os.path.join(files, "studentplan", "2024")
            os.makedirs(folder, exist_ok=True)
            with open(os.path.join(folder, f"sp-{plan_id}_f-{plan_id}_st-{code}_IEP.docx"), "wb") as f:
                f.write(b"PK\x03\x04 fake docx")
        if n % 6 == 0:
            gh_id += 1
            add("goalheader", id=str(gh_id), studentID=sid, studentCode=code,
                overview="Invented goals overview.", updated_date=stamp(base))
            goal_id += 1
            add("goal", id=str(goal_id), goalheader=str(gh_id), notes="Read more.",
                student_notes="I will try.", year="2024", updated_date=stamp(base))
        # correspondence: one email to 3 staff (logged per recipient), one SMS to a parent
        d = base + timedelta(days=n)
        for r in range(3):
            corr_id += 1
            corr_rows.append({"id": str(corr_id), "type": "email", "subject":
                              f"[TA:PC] Academic entered for {first} {last}",
                              "contents": f"<html><head><style>p{{}}</style></head><body><p>Invented "
                                          f"notification about {first}. <a href='http://x'>link</a>"
                                          f"<img src='http://x/logo.png'></p></body></html>",
                              "updated_date": ts(d, 10, 5), "cmessage": ""})
            part_rows += [
                {"id": str(part_id), "log_correspondence": str(corr_id), "address": "",
                 "person_id": staff[0][0], "person_type": "staff", "role": "sender",
                 "success": "t", "updated_date": stamp(d)},
                {"id": str(part_id + 1), "log_correspondence": str(corr_id),
                 "address": f"teacher{r + 2}@example.invalid", "person_id": staff[r + 1][0],
                 "person_type": "staff", "role": "recipient", "success": "t" if r else "f",
                 "updated_date": stamp(d)},
                {"id": str(part_id + 2), "log_correspondence": str(corr_id), "address": "",
                 "person_id": sid, "person_type": "student", "role": "context",
                 "success": "t", "updated_date": stamp(d)}]
            part_id += 3
        corr_id += 1
        corr_rows.append({"id": str(corr_id), "type": "sms", "subject": "",
                          "contents": f"{first} was absent today (invented SMS).",
                          "updated_date": ts(d, 15), "cmessage": ""})
        part_rows += [{"id": str(part_id), "log_correspondence": str(corr_id),
                       "address": "0400000001", "person_id": str(contact_id),
                       "person_type": "contact", "role": "recipient", "success": "t",
                       "updated_date": stamp(d)},
                      {"id": str(part_id + 1), "log_correspondence": str(corr_id), "address": "",
                       "person_id": sid, "person_type": "student", "role": "context",
                       "success": "t", "updated_date": stamp(d)}]
        part_id += 2
        if n % 4 == 1:
            msg_id += 1
            msg_rows.append({"id": str(msg_id), "subject": f"Invented message {msg_id}",
                             "contents": f"<p>Hello {first}, invented message body.</p>",
                             "sender_name": staff[1][2], "sender": staff[1][0], "blind": "f",
                             "updated_date": stamp(d)})
            add("cmessageparticipant", id=str(msgp_id), cmessage=str(msg_id), studentID=sid,
                studentCode=code, label="inbox", read="t")
            msgp_id += 1
            folder = os.path.join(files, "cmessage", "2024")
            os.makedirs(folder, exist_ok=True)
            with open(os.path.join(folder, f"m-{msg_id}_f-{msg_id}_worksheet.pdf"), "wb") as f:
                f.write(b"%PDF-1.4\n% fake\n")

    # SIP document keyed by student code only
    folder = os.path.join(files, "sip", "2024")
    os.makedirs(folder, exist_ok=True)
    with open(os.path.join(folder, "sip_st_9003_f_Invented_Plan.pdf"), "wb") as f:
        f.write(b"%PDF-1.4\n% fake\n")

    # ---- write CSVs (with BOM, like SEQTA)
    drop_cols = {}
    for dc in a.drop_column:
        t, c = dc.split(".", 1)
        drop_cols.setdefault(t + ".csv", set()).add(c)

    def write(fname, cols, data_rows):
        if fname[:-4] in a.drop_table:
            return
        cols = [c for c in cols if c not in drop_cols.get(fname, set())]
        with open(os.path.join(data, fname), "w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            w.writerow(cols)
            for r in data_rows:
                w.writerow([r.get(c, "") for c in cols])

    for fname, cols in SCHEMA.items():
        if "*" in fname:
            continue
        write(fname, cols, rows.get(fname, []))
    write("cmessage_2024.csv", CMSG_COLS, msg_rows)
    write("log_correspondence_2024.csv", CORR_COLS, corr_rows)
    write("log_correspondence_participant.csv", PART_COLS, part_rows)
    print(f"fake export written to {root}")
    return root


if __name__ == "__main__":
    main()
