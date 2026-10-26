"""Idempotently copy the current SQLite database to Cloud Firestore.

Usage:
    python tools/migrate_sqlite_to_firebase.py --execute

Without --execute it only prints record counts. Existing Firestore documents
with the same IDs are merged, so an interrupted migration can be rerun.
"""

import argparse
import hashlib
import os
import sqlite3
import sys
from pathlib import Path

from dotenv import load_dotenv


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")


def rows(connection, sql):
    cursor = connection.execute(sql)
    columns = [item[0] for item in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--execute", action="store_true", help="write data to Firestore")
    parser.add_argument("--database", default=os.getenv("DB_PATH", str(ROOT / "data.db")))
    args = parser.parse_args()

    connection = sqlite3.connect(args.database)
    users = rows(connection, "SELECT * FROM users")
    chats = rows(connection, "SELECT * FROM chats")
    messages = rows(connection, "SELECT * FROM messages ORDER BY id")
    matches = rows(connection, "SELECT * FROM matches ORDER BY id")
    sessions = rows(connection, "SELECT * FROM sessions")
    subs = rows(connection, "SELECT chat_id, sub_id FROM chat_subs ORDER BY rowid")
    topics = rows(connection, "SELECT key, title FROM topics")
    subtopics = rows(connection, "SELECT topic_key, id, grp, title FROM subtopics")

    counts = {"users": len(users), "chats": len(chats), "messages": len(messages),
              "matches": len(matches), "sessions": len(sessions), "topics": len(topics),
              "subtopics": len(subtopics)}
    print("SQLite records:", ", ".join(f"{key}={value}" for key, value in counts.items()))
    if not args.execute:
        print("Dry run only. Add --execute to copy these records.")
        return

    from firebase_db import FirebaseDB

    target = FirebaseDB()
    client = target.client
    subs_by_chat = {}
    for item in subs:
        subs_by_chat.setdefault(item["chat_id"], []).append(item["sub_id"])

    for item in topics:
        client.collection("topics").document(item["key"]).set({"title": item["title"]}, merge=True)
    for item in subtopics:
        (client.collection("topics").document(item["topic_key"]).collection("subtopics")
         .document(item["id"]).set({"group": item["grp"], "title": item["title"]}, merge=True))

    for item in users:
        uid = int(item.pop("uid"))
        item["push"] = bool(item.get("push", 1))
        client.collection("users").document(str(uid)).set(item, merge=True)
        email = item.get("email")
        if email:
            normalized = email.strip().lower()
            key = hashlib.sha256(normalized.encode()).hexdigest()
            client.collection("email_index").document(key).set({"uid": uid, "email": normalized})
        if item.get("telegram_id") is not None:
            client.collection("telegram_index").document(str(item["telegram_id"])).set({"uid": uid})

    for item in chats:
        cid = int(item.pop("id"))
        item["subs"] = subs_by_chat.get(cid, [])
        client.collection("chats").document(str(cid)).set(item, merge=True)
    for item in messages:
        mid, cid = int(item.pop("id")), int(item.pop("chat_id"))
        (client.collection("chats").document(str(cid)).collection("messages")
         .document(str(mid)).set(item, merge=True))
    for item in matches:
        mid = int(item.pop("id"))
        client.collection("matches").document(str(mid)).set(item, merge=True)
    for item in sessions:
        token_hash = item.pop("token_hash")
        client.collection("sessions").document(token_hash).set(item, merge=True)

    counter_ref = client.collection("meta").document("counters")
    existing_counters = counter_ref.get().to_dict() or {}
    sqlite_user_id = max((int(item["uid"]) for item in rows(connection, "SELECT uid FROM users")), default=0)
    sqlite_chat_id = max((int(item["id"]) for item in rows(connection, "SELECT id FROM chats")), default=0)
    counter_ref.set({
        "user_id": max(int(existing_counters.get("user_id", 0)), sqlite_user_id),
        "chat_id": max(int(existing_counters.get("chat_id", 0)), sqlite_chat_id),
    }, merge=True)

    cloud_users = list(client.collection("users").stream())
    cloud_chats = list(client.collection("chats").stream())
    cloud_messages = sum(
        1 for chat in cloud_chats for _ in chat.reference.collection("messages").stream()
    )
    if len(cloud_users) < len(users) or len(cloud_chats) < len(chats) or cloud_messages < len(messages):
        raise RuntimeError("Firestore verification failed: record count is lower than SQLite")
    print(f"Migration verified: users={len(cloud_users)}, chats={len(cloud_chats)}, "
          f"messages={cloud_messages}.")


if __name__ == "__main__":
    main()
