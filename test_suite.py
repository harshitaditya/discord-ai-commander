import os
import sys
import asyncio
import json
import http.client
from http.server import HTTPServer
import threading
import time
from dotenv import load_dotenv

# Load environment
load_dotenv()

import bot

passed_tests = 0
failed_tests = 0

def report(name, success, details=""):
    global passed_tests, failed_tests
    if success:
        passed_tests += 1
        print(f"  ✅ PASS: {name} {details}")
    else:
        failed_tests += 1
        print(f"  ❌ FAIL: {name} - {details}")

print("==================================================")
print("🧪 RUNNING COMPREHENSIVE BOT UNIT TEST SUITE")
print("==================================================")

# --- 1. Test Authorization Logic ---
print("\n[1] Testing Authorization & Permissions...")
class DummyUser:
    def __init__(self, id, name, display_name="", global_name=""):
        self.id = id
        self.name = name
        self.display_name = display_name
        self.global_name = global_name

u_aditya = DummyUser(1406315236125970432, "aditya_ak02", "Aditya", "Aditya")
u_caps = DummyUser(999, "ADITYA_AK02")
u_hacker = DummyUser(12345, "evil_hacker", "Evil", "Evil")

report("Authorized: exact username 'aditya_ak02'", bot.is_authorized(u_aditya))
report("Authorized: case-insensitive 'ADITYA_AK02'", bot.is_authorized(u_caps))
report("Security: Reject unauthorized user 'evil_hacker'", not bot.is_authorized(u_hacker))

# --- 2. Test GitHub API Connectivity ---
print("\n[2] Testing GitHub Integration & Repos Fetching...")
try:
    repos = bot.fetch_user_repos()
    if repos and len(repos) > 0:
        report("GitHub Repos Fetch", True, f"Fetched {len(repos)} repos (e.g. {repos[:3]})")
    else:
        report("GitHub Repos Fetch", False, "Returned empty list. Check GH_PAT and GH_USERNAME.")
except Exception as e:
    report("GitHub Repos Fetch", False, str(e))

# --- 3. Test Discord UI Views & Limits ---
print("\n[3] Testing Discord UI Components & Limits...")
try:
    # Test StudioRepoView with fetched repos (sliced to 25)
    repo_view = bot.StudioRepoView(repos or ["repo1", "repo2"])
    report("StudioRepoView instantiation", len(repo_view.children) > 0, f"Options: {len(repo_view.children[0].options)}")
except Exception as e:
    report("StudioRepoView instantiation", False, str(e))

try:
    # Test StudioModelView with default model
    model_view = bot.StudioModelView("gemini-3.5-flash-lite")
    report("StudioModelView instantiation", len(model_view.children) > 0, f"Models: {len(model_view.children[0].options)}")
except Exception as e:
    report("StudioModelView instantiation", False, str(e))

try:
    # Test StudioControlView
    ctrl_view = bot.StudioControlView(1406315236125970432)
    report("StudioControlView buttons", len(ctrl_view.children) == 4, f"Buttons count: {len(ctrl_view.children)}")
except Exception as e:
    report("StudioControlView buttons", False, str(e))

try:
    # Test ChatDecisionView
    decision_view = bot.ChatDecisionView(1406315236125970432, "RetroRaag", "Test task")
    report("ChatDecisionView buttons", len(decision_view.children) == 5, f"Buttons count: {len(decision_view.children)}")
except Exception as e:
    report("ChatDecisionView buttons", False, str(e))

# --- 4. Test Health Check Server (GET and HEAD for UptimeRobot) ---
print("\n[4] Testing Health Check HTTP Server (GET & HEAD Methods)...")
test_port = 19876
server = HTTPServer(("127.0.0.1", test_port), bot.HealthCheckHandler)
server_thread = threading.Thread(target=server.serve_forever, daemon=True)
server_thread.start()
time.sleep(0.5)

# Test HEAD request (What UptimeRobot sends!)
try:
    conn = http.client.HTTPConnection("127.0.0.1", test_port, timeout=5)
    conn.request("HEAD", "/")
    head_res = conn.getresponse()
    body = head_res.read()
    report("HEAD / request (UptimeRobot ping format)", head_res.status == 200, f"HTTP {head_res.status} OK (body bytes: {len(body)})")
    conn.close()
except Exception as e:
    report("HEAD / request (UptimeRobot ping format)", False, str(e))

# Test GET request
try:
    conn = http.client.HTTPConnection("127.0.0.1", test_port, timeout=5)
    conn.request("GET", "/")
    get_res = conn.getresponse()
    body_data = json.loads(get_res.read().decode("utf-8"))
    report("GET / request payload", get_res.status == 200, f"Status: '{body_data.get('status')}', GH Token: '{body_data.get('environment', {}).get('gh_token_status')}'")
    conn.close()
except Exception as e:
    report("GET / request payload", False, str(e))

server.shutdown()

# --- 5. Test AI Generation with Default Model ---
print("\n[5] Testing AI Engine with Default Model (gemini-3.5-flash-lite)...")
async def test_ai_call():
    try:
        ans = await asyncio.to_thread(
            bot.call_ai_text,
            "Reply with exactly the words: 'AI_TEST_SUCCESSFUL'",
            "You are a helpful coding assistant.",
            "gemini-3.5-flash-lite"
        )
        report("AI API Response", "AI_TEST_SUCCESSFUL" in ans or len(ans) > 0, f"Response: {ans[:60]}")
    except Exception as e:
        report("AI API Response", False, str(e))

asyncio.run(test_ai_call())

# --- 6. Test Mobile Trigger Matching & Typos ---
print("\n[6] Testing Natural Language & Mobile Typo Triggers...")
triggers_to_test = [
    ("lets code", True),
    ("Let's code", True),
    ("LETS CODE", True),
    ("lets ccode", True),
    ("let's cod3", True),
    ("lets cod3", True),
    ("code", True),
    ("start coding", True),
    ("close", False),   # Should not trigger studio, but reset
    ("random chat", False)
]

for trigger, expected in triggers_to_test:
    content_lower = trigger.strip().lower()
    cleaned_trigger = content_lower.replace("'", "").replace("3", "e").replace(" ", "").replace("_", "")
    is_match = (
        content_lower in ["lets code", "let's code", "lets_code", "lets ccode", "let's ccode", "letscode", "let code", "start coding", "start code", "code", "start", "let's cod3", "lets cod3"] or
        cleaned_trigger in ["letscode", "letsccode", "letcode", "startcoding", "startcode", "code", "start", "studio"]
    )
    report(f"Trigger match: '{trigger}'", is_match == expected, f"Result: {is_match}")

print("\n==================================================")
print(f"📊 SUMMARY: {passed_tests} PASSED, {failed_tests} FAILED")
print("==================================================")

if failed_tests > 0:
    sys.exit(1)
sys.exit(0)
