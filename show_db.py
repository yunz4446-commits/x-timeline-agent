import sqlite3

conn = sqlite3.connect("data/timeline.db")

# Latest 30 tweets, full text
rows = conn.execute(
    "SELECT tweet_created_at, author_username, is_retweet, is_reply, length(text), text "
    "FROM tweets ORDER BY tweet_created_at DESC LIMIT 30"
).fetchall()

print(f"{'time':19s} | {'@author':20s} | rt | rp | len  | full text")
print("-" * 130)

for r in rows:
    ts = str(r[0] or "")[:19]
    author = r[1][:20]
    rt = r[2]
    rp = r[3]
    length = r[4]
    text = r[5]
    print(f"{ts:19s} | {author:20s} | {rt:2d} | {rp:2d} | {length:4d} | {text}")
    print()

# Summary
total = conn.execute("SELECT COUNT(*) FROM tweets").fetchone()[0]
retweets = conn.execute("SELECT COUNT(*) FROM tweets WHERE is_retweet=1").fetchone()[0]
long = conn.execute("SELECT COUNT(*) FROM tweets WHERE length(text) > 200").fetchone()[0]
print(f"Total: {total} tweets | retweets: {retweets} | long(>200chars): {long}")
conn.close()
