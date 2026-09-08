import requests

r = requests.post(
    "http://localhost:8069/api/moderation/check-text",
    json={"jsonrpc": "2.0", "method": "call", "params": {"text": "I hate you, you are worthless garbage"}},
    headers={"Content-Type": "application/json"}
)
print("Status code:", r.status_code)
print("Raw response:")
print(r.text)