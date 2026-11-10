
import os
from pathlib import Path

import pandas as pd
import requests
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

TOKEN = os.getenv("EODHD_API_TOKEN", "").strip()

if not TOKEN:
    raise SystemExit("API token missing. Check your .env file.")

companies_file = BASE_DIR / "egx_companies.csv"

if not companies_file.exists():
    raise SystemExit("egx_companies.csv was not found.")

companies = pd.read_csv(companies_file, dtype={"Code": str})
companies = companies.dropna(subset=["Code"]).head(5)

results = []

for _, company in companies.iterrows():
    symbol = company["Code"].strip() + ".EGX"
    url = f"https://eodhd.com/api/real-time/{symbol}"

    print(f"\nTesting {company['Name']} ({symbol})")

    try:
        response = requests.get(
            url,
            params={"api_token": TOKEN, "fmt": "json"},
            timeout=30,
        )

        print("HTTP Status:", response.status_code)

        if response.ok:
            data = response.json()

            if isinstance(data, dict):
                print("Response:", data)

                results.append({
                    "Code": company["Code"],
                    "Name": company["Name"],
                    "Symbol": symbol,
                    "Status": "OK",
                    "Price": data.get("close"),
                    "Timestamp": data.get("timestamp"),
                    "Response": str(data),
                })
            else:
                print("Unexpected response:", data)

        else:
            print("API message:", response.text[:300])

            results.append({
                "Code": company["Code"],
                "Name": company["Name"],
                "Symbol": symbol,
                "Status": f"HTTP {response.status_code}",
                "Price": None,
                "Timestamp": None,
                "Response": response.text[:300],
            })

    except requests.RequestException as error:
        print("Request error:", error)

if results:
    output = BASE_DIR / "egx_price_test.csv"
    pd.DataFrame(results).to_csv(
        output, index=False, encoding="utf-8-sig"
    )
    print(f"\nResults saved to: {output}")
