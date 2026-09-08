import xmlrpc.client
import time
import base64

# -----------------------------
# ODOO CONFIGURATION
# -----------------------------
url = "http://localhost:8069"
db = "mydatabase"
username = "admin"  # <-- CHANGED: matches the account the API key was generated under
api_key = "03ccfb36462f6d8f654465d871d8552405eda63c"

# Help forum
forum_id = 1

# -----------------------------
# 1. AUTHENTICATE
# -----------------------------
common = xmlrpc.client.ServerProxy(
    f"{url}/xmlrpc/2/common"
)
uid = common.authenticate(
    db,
    username,
    api_key,
    {}
)
print(f"[1] Authenticated. UID: {uid}")
if not uid:
    print("Authentication failed!")
    exit()

# -----------------------------
# 2. CONNECT TO ODOO OBJECT API
# -----------------------------
models = xmlrpc.client.ServerProxy(
    f"{url}/xmlrpc/2/object"
)

# -----------------------------
# 3. CREATE CLEAN POST
# -----------------------------
clean_post_id = models.execute_kw(
    db,
    uid,
    api_key,
    "forum.post",
    "create",
    [{
        "name": "API Test - Clean Post",
        "content": "What is the best way to learn Python?",
        "forum_id": forum_id,
    }]
)
print(f"[2] Clean post created. ID = {clean_post_id}")

# -----------------------------
# 4. CREATE TOXIC POST
# -----------------------------
toxic_post_id = models.execute_kw(
    db,
    uid,
    api_key,
    "forum.post",
    "create",
    [{
        "name": "API Test - Toxic Post",
        "content": "I hate you, you are worthless garbage.",
        "forum_id": forum_id,
    }]
)
print(f"[3] Toxic post created. ID = {toxic_post_id}")

# -----------------------------
# 5. WAIT FOR MODERATION
# -----------------------------
time.sleep(2)

# -----------------------------
# 6. CHECK POST STATES
# -----------------------------
results = models.execute_kw(
    db,
    uid,
    api_key,
    "forum.post",
    "read",
    [[clean_post_id, toxic_post_id]],
    {
        "fields": ["id", "name", "state"]
    }
)
print("\n[4] Moderation Results")
for post in results:
    print(
        f"Post {post['id']} - "
        f"{post['name']} - "
        f"state = {post['state']}"
    )

# -----------------------------
# 7. CHECK MODERATION LOG
# -----------------------------
messages = models.execute_kw(
    db,
    uid,
    api_key,
    "mail.message",
    "search_read",
    [[
        ["res_id", "=", toxic_post_id],
        ["model", "=", "forum.post"]
    ]],
    {
        "fields": ["body"],
        "limit": 5
    }
)
print("\n[5] Moderation Log")
for message in messages:
    print(message["body"])

# -----------------------------
# 8. TEST IMAGE ATTACHMENT HOOK
# -----------------------------
image_path = r"C:\Users\HP\Desktop\dealwallet\test.png"
with open(image_path, "rb") as f:
    image_data = base64.b64encode(f.read()).decode()

attachment_id = models.execute_kw(
    db,
    uid,
    api_key,
    "ir.attachment",
    "create",
    [{
        "name": "test_attachment.png",
        "datas": image_data,
        "res_model": "forum.post",
        "res_id": clean_post_id,
        "mimetype": "image/png",
    }]
)
print(f"\n[6] Attachment created. ID = {attachment_id}")

# -----------------------------
# 9. CHECK POST STATE AFTER ATTACHMENT
# -----------------------------
result = models.execute_kw(
    db,
    uid,
    api_key,
    "forum.post",
    "read",
    [[clean_post_id]],
    {
        "fields": ["id", "state"]
    }
)
print(f"[7] Post {clean_post_id} state after attachment: {result[0]['state']}")