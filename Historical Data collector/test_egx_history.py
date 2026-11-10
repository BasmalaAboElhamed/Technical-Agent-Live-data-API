
import os
from pathlib import Path

import pandas as pd
import requests
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

token = os.getenv("EODHD_API_TOKEN", "").strip()
if not token:
    raise SystemExit("Missing API token in .env")

companies = pd.read_csv(
    BASE_DIR / "egx_companies.csv",
    dtype={"Code": str}
).head(5)

for _, company in companies.iterrows():
    symbol = f"{company['Code']}.EGX"
    url = f"https://eodhd.com/api/eod/{symbol}"

    print(f"\nTesting {symbol} - {company['Name']}")

    try:
        response = requests.get(
            url,
            params={
                "api_token": token,
                "fmt": "json",
                "period": "d",
                "order": "d",
                "from": "2026-09-01",
                "to": "2026-10-13",
            },
            timeout=30,
        )

        print("HTTP Status:", response.status_code)

        if response.ok:
            data = response.json()

            if isinstance(data, list) and data:
                print("Latest available record:", data[0])
            else:
                print("No historical records returned:", data)
        else:
            print("API response:", response.text[:500])

    except (requests.RequestException, ValueError) as error:
        print("Error:", error)
