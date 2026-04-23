#!/usr/bin/env python3
"""Daily job matcher using Anthropic API"""

import os
import json
from anthropic import Anthropic

def run_job_matcher():
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise ValueError("ANTHROPIC_API_KEY not set")
    
    client = Anthropic()
    
    system_prompt = """You are a job search assistant. Search for junior full-stack and backend roles.
    Filter by: React, TypeScript, Python, FastAPI, Node.js, Docker (skills).
    Required experience: max 2.5 years.
    Location: Central Israel (Tel Aviv, Ramat Gan, Herzliya, etc.) or remote-Israel.
    Skip: Experis, Infinity Labs, bootcamps, senior roles.
    
    Return JSON:
    {"jobs_found": N, "jobs_saved": M, "status": "success", "message": "..."}"""
    
    messages = [
        {"role": "user", "content": "Search job boards and save matches to Google Sheets (1zk3vAgTwKmcn4xgwrue54KyUqRYSlvrkr9azOWeCKno). Return JSON."}
    ]
    
    print("Starting job matcher...")
    
    response = client.messages.create(
        model="claude-opus-4-20250805",
        max_tokens=4096,
        system=system_prompt,
        messages=messages
    )
    
    result_text = response.content[0].text
    print("Job matcher response:")
    print(result_text)
    
    try:
        json_start = result_text.find('{')
        json_end = result_text.rfind('}') + 1
        if json_start >= 0 and json_end > json_start:
            json_str = result_text[json_start:json_end]
            result = json.loads(json_str)
            print(f"\n✓ Found {result.get('jobs_found', 0)} jobs, saved {result.get('jobs_saved', 0)}")
            return result
    except json.JSONDecodeError:
        pass
    
    return {"status": "completed", "message": result_text}

if __name__ == "__main__":
    try:
        run_job_matcher()
        print("\nJob matcher completed successfully")
        exit(0)
    except Exception as e:
        print(f"\nError: {e}")
        exit(1)