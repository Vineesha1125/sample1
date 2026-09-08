import base64
import requests

with open("test.png", "rb") as f:
    image_data = base64.b64encode(f.read()).decode()

r = requests.post(
    "http://localhost:8069/api/moderation/check-image",
    json={
        "jsonrpc": "2.0",
        "method": "call",
        "params": {
            "image_base64": image_data,
            "filename": "test.png",
            "mimetype": "image/png"
        }
    },
    headers={"Content-Type": "application/json"}
)
print("Status code:", r.status_code)
print(r.json())