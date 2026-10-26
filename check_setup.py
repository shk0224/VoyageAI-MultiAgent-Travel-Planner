"""Check your API keys and LLM setup, and say exactly what is wrong.

    python check_setup.py

What it does (uses almost no quota):
1. Shows which keys are set in .env (masked).
2. Sends one tiny message to Groq.
3. Tries the trip-extraction form with each structured-output method and
   tells you which one works -> put that in .env as GROQ_STRUCTURED_METHOD.
4. Checks your SerpApi key with the free account endpoint (no search used).
"""
import os
import sys
import traceback
from datetime import date

from dotenv import load_dotenv

load_dotenv()
if os.getenv("USE_CERTIFI", "0") == "1":
    import certifi
    os.environ["SSL_CERT_FILE"] = certifi.where()
    os.environ["REQUESTS_CA_BUNDLE"] = certifi.where()


def mask(value):
    return "MISSING" if not value else f"set ({value[:4]}…{value[-3:]}, {len(value)} chars)"


def explain(exc: Exception) -> str:
    text = f"{type(exc).__name__}: {str(exc)[:400]}"
    low = text.lower()
    if "ssl" in low or "certificate" in low:
        text += "\n   -> Looks like an SSL/certificate problem. Set USE_CERTIFI=1 in .env and run again."
    elif "401" in low or "invalid api key" in low or "authentication" in low:
        text += "\n   -> The API key was rejected. Copy it again from the provider's console."
    elif "404" in low or "model" in low and "not" in low and "found" in low:
        text += "\n   -> The model name may be wrong or not available on your account (GROQ_MODEL)."
    elif "429" in low or "rate limit" in low:
        text += "\n   -> Rate limit reached. Wait a minute and try again."
    elif "connect" in low or "timeout" in low or "proxy" in low:
        text += "\n   -> Network problem reaching the API (internet, VPN, firewall or proxy)."
    return text


print("== 1. Keys in .env ==")
for name in ("GROQ_API_KEY", "SERPAPI_API_KEY", "TAVILY_API_KEY"):
    print(f"   {name}: {mask(os.getenv(name))}")
model = os.getenv("GROQ_MODEL", "openai/gpt-oss-20b")
print(f"   GROQ_MODEL: {model}")
print(f"   GROQ_STRUCTURED_METHOD: {os.getenv('GROQ_STRUCTURED_METHOD', 'function_calling')}")
print(f"   USE_CERTIFI: {os.getenv('USE_CERTIFI', '0')}")

if not os.getenv("GROQ_API_KEY"):
    sys.exit("\nGROQ_API_KEY is missing. Add it to .env first.")

from langchain_groq import ChatGroq  # noqa: E402

from core.models import TripRequest  # noqa: E402

llm = ChatGroq(model=model, api_key=os.getenv("GROQ_API_KEY"), temperature=0, max_retries=0)

print("\n== 2. Plain Groq call ==")
try:
    reply = llm.invoke("Reply with the single word OK.")
    print(f"   OK -> model replied: {reply.content!r}")
except Exception as exc:
    print("   FAILED:", explain(exc))
    sys.exit("\nFix this first; nothing else can work until a plain call succeeds.")

print("\n== 3. Structured trip extraction (what the app does first) ==")
message = ("Plan a 4-day trip from Atlanta, Georgia to New York City, December 10 to 13, 2026, "
           f"for 2 adults, budget $1,500. Today is {date.today().isoformat()}.")
working = []
for method in ("function_calling", "json_schema", "json_mode"):
    try:
        prompt = message if method != "json_mode" else message + " Reply in JSON matching the TripRequest fields."
        result = llm.with_structured_output(TripRequest, method=method).invoke(prompt)
        if isinstance(result, dict):
            result = TripRequest(**result)
        print(f"   {method:17s} OK -> {result.origin_city} -> {result.destination_city}, "
              f"{result.departure_date} to {result.return_date}, adults={result.adults}, budget={result.budget_usd}")
        working.append(method)
    except Exception as exc:
        print(f"   {method:17s} FAILED: {explain(exc)}")
        if os.getenv("DEBUG"):
            traceback.print_exc()

if working:
    best = working[0]
    print(f"\n   -> Use GROQ_STRUCTURED_METHOD={best} in your .env"
          + ("" if best == os.getenv("GROQ_STRUCTURED_METHOD", "function_calling") else "  (different from now!)"))
else:
    print("\n   -> No method worked with this model. Try GROQ_MODEL=openai/gpt-oss-120b "
          "or another Groq model that supports tool use, then run this again.")

print("\n== 4. SerpApi key (free account check, no search used) ==")
key = os.getenv("SERPAPI_API_KEY")
if not key:
    print("   SKIPPED: SERPAPI_API_KEY missing")
else:
    import requests
    try:
        r = requests.get("https://serpapi.com/account.json", params={"api_key": key}, timeout=20)
        data = r.json()
        if r.ok and "error" not in data:
            left = data.get("total_searches_left", data.get("plan_searches_left", "?"))
            print(f"   OK -> searches left this month: {left}")
        else:
            print(f"   FAILED: HTTP {r.status_code} {data.get('error', '')}")
    except Exception as exc:
        print("   FAILED:", explain(exc))

print("\nDone. Send this whole output if you need help.")
