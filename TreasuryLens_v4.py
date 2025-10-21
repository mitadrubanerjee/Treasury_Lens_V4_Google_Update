# app.py

import os
import re
import json
import streamlit as st
import requests
from typing import List, Dict, Tuple
import plotly.express as px
from openai import OpenAI
from bs4 import BeautifulSoup
import feedparser
from datetime import datetime, timedelta
import html
import xml.etree.ElementTree as ET
from urllib.parse import quote_plus, urlparse

# ── Streamlit Config ───────────────────────────────────────
st.set_page_config(page_title="TreasuryLens", layout="wide", initial_sidebar_state="expanded")

# ── Global CSS Injection ───────────────────────────────────
st.markdown("""
<style>
h1, h2, h3 { text-align: center; }
</style>
<style>
  .card { background-color: #1f2937; padding: 1rem; border-radius: 0.5rem; margin-bottom: 1rem; }
  .card h3 { margin-top: 0; color: #ffffff; }
  .card ul { padding-left: 1.2rem; }
  .card li { color: #e5e7eb; margin-bottom: 0.4rem; }
  .metric-positive { background-color: #cfe8fc; color: #1e3a8a; padding: 0.4rem 0.8rem; border-radius: 0.4rem; margin-bottom: 0.8rem; display: inline-block; font-size: 1rem; }
  .metric-neutral { background-color: #fceecf; color: #78350f; padding: 0.4rem 0.8rem; border-radius: 0.4rem; margin-bottom: 0.8rem; display: inline-block; font-size: 1rem; }
  .metric-negative { background-color: #fcdede; color: #7f1d1d; padding: 0.4rem 0.8rem; border-radius: 0.4rem; margin-bottom: 0.8rem; display: inline-block; font-size: 1rem; }
</style>
""", unsafe_allow_html=True)

# ── API Keys ───────────────────────────────────────────────
openai_api_key = st.secrets["openai"]["api_key"]
tradingeconomics_api_key = st.secrets["tradingeconomics"]["api_key"]

client = OpenAI(api_key=openai_api_key) if openai_api_key else None

# ── Fetch Economic Calendar ────────────────────────────────
@st.cache_data(show_spinner=False)
def scrape_calendar() -> List[Dict]:
    today = datetime.today()
    end_date = today + timedelta(days=4)
    url = "https://api.tradingeconomics.com/calendar"
    params = {
        "c": tradingeconomics_api_key,
        "country": "united states,eurozone,united kingdom,japan,china",
        "start_date": today.strftime("%Y-%m-%d"),
        "end_date": end_date.strftime("%Y-%m-%d")
    }
    try:
        r = requests.get(url, params=params, timeout=10)
        r.raise_for_status()
        data = r.json()
        events = []
        for item in data:
            try:
                dt = datetime.strptime(item["Date"], "%Y-%m-%dT%H:%M:%S")
                events.append({
                    "date": dt.strftime("%Y-%m-%d"),
                    "weekday": dt.strftime("%a"),
                    "region": item.get("Country", "Unknown"),
                    "event": item.get("Category", "Event"),
                })
            except:
                continue
        return events
    except Exception as e:
        st.error(f"TradingEconomics API error: {e}")
        return []

# ── Google News RSS Helpers ───────────────────────────────
GOOGLE_NEWS_RSS = "https://news.google.com/rss/search?q={query}&hl=en-US&gl=US&ceid=US:en"

def _guess_source(title: str, link: str) -> str:
    parts = title.rsplit(" - ", 1)
    if len(parts) == 2 and 2 <= len(parts[1]) <= 40:
        return parts[1].strip()
    netloc = urlparse(link).netloc.lower()
    domain_map = {
        "www.bloomberg.com": "Bloomberg","bloomberg.com": "Bloomberg",
        "www.reuters.com": "Reuters","reuters.com": "Reuters",
        "www.cnbc.com": "CNBC","cnbc.com": "CNBC",
        "www.ft.com": "Financial Times","ft.com": "Financial Times",
        "www.theguardian.com": "Guardian","theguardian.com": "Guardian",
        "finance.yahoo.com": "Yahoo Finance",
        "www.barrons.com": "Barrons","www.bbc.co.uk": "BBC","www.bbc.com": "BBC",
        "www.marketwatch.com": "Market Watch","www.economist.com": "Economist",
        "asia.nikkei.com": "Nikkei","www.ecb.europa.eu": "ECB","www.federalreserve.gov": "Fed",
        "www.fxstreet.com": "FXStreet","www.forexlive.com": "Forex Live","www.zerohedge.com": "Zero Hedge",
    }
    return domain_map.get(netloc, netloc or "Unknown")

# ── Global Headline Fetcher ───────────────────────────────
@st.cache_data(ttl=3600, show_spinner=False)
def fetch_global_headlines(desired_count: int = 10, max_offset: int = 120, allowed_sources: set = None) -> Tuple[List[Dict], bool]:
    if allowed_sources is None:
        allowed_sources = {
            "Bloomberg","Reuters","CNBC","Financial Times","Guardian",
            "Yahoo Finance","Barrons","BBC","Forex Live","FXStreet",
            "Market Watch","Economist","Nikkei","Fed","ECB","Zero Hedge","Yahoo"
        }
    try:
        query = "forex market news when:1d"
        url = GOOGLE_NEWS_RSS.format(query=quote_plus(query))
        resp = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
        root = ET.fromstring(resp.content)
        items = root.findall(".//item")
        all_articles, filtered, seen = [], [], set()
        for it in items:
            title = html.unescape((it.findtext("title") or "").strip())
            desc  = html.unescape((it.findtext("description") or "").strip())
            link  = (it.findtext("link") or "").strip()
            if not link or link in seen: continue
            seen.add(link)
            source = _guess_source(title, link)
            article = {"title": title,"description": desc,"source": source,"url": link}
            all_articles.append(article)
            if source in allowed_sources: filtered.append(article)
            if len(filtered) >= desired_count: break
        if len(filtered) >= desired_count: return filtered[:desired_count], False
        return all_articles[:desired_count], True
    except Exception as e:
        st.error(f"Error fetching global headlines (RSS): {e}")
        return [], True


# ── Currency Headline Fetcher ─────────────────────────────
@st.cache_data(ttl=3600, show_spinner=False)
def fetch_currency_headlines(pair: str, desired_count: int = 10, max_offset: int = 120, allowed_sources: set = None) -> Tuple[List[Dict], bool]:
    if allowed_sources is None:
        allowed_sources = {
            "Bloomberg","Reuters","CNBC","Financial Times","Guardian",
            "Yahoo Finance","Barrons","BBC","Forex Live","FXStreet",
            "Market Watch","Economist","Nikkei","Fed","ECB","Zero Hedge","Yahoo"
        }
    try:
        query = f"{pair} forex news when:7d"
        url = GOOGLE_NEWS_RSS.format(query=quote_plus(query))
        resp = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
        root = ET.fromstring(resp.content)
        items = root.findall(".//item")
        all_articles, filtered, seen = [], [], set()
        for it in items:
            title = html.unescape((it.findtext("title") or "").strip())
            desc  = html.unescape((it.findtext("description") or "").strip())
            link  = (it.findtext("link") or "").strip()
            if not link or link in seen: continue
            seen.add(link)
            source = _guess_source(title, link)
            article = {"title": title,"description": desc,"source": source,"url": link}
            all_articles.append(article)
            if source in allowed_sources: filtered.append(article)
            if len(filtered) >= desired_count: break
        if len(filtered) >= desired_count: return filtered[:desired_count], False
        return all_articles[:desired_count], True
    except Exception as e:
        st.error(f"Error fetching headlines for {pair} (RSS): {e}")
        return [], True

# ── Text Cleaner ──────────────────────────────────────────
def clean_text(text: str) -> str:
    text = re.sub(r'(?<=\d)(?=(billion|million|trillion))', r' ', text, flags=re.IGNORECASE)
    text = re.sub(r'(?<=[a-z])(?=[A-Z])', r' ', text)
    text = re.sub(r'\*{2,}', '**', text)
    return text.strip()

# ── GPT Analysis ──────────────────────────────────────────
@st.cache_data(ttl=3600, show_spinner=False)
@st.cache_data(ttl=3600, show_spinner=False)
def analyze_with_gpt(snippets: List[str]) -> Tuple[List[str], str, Dict[str, int], str]:
    # 0) No headlines → neutral stub (keeps your previous behavior)
    if not snippets:
        return [], "neutral", {"positive": 0, "neutral": 0, "negative": 0}, "No headlines available to analyze."

    if client is None or not isinstance(client, OpenAI):
        st.error("OpenAI client is not configured. Add your OPENAI key.")
        return [], "neutral", {"positive": 0, "neutral": 0, "negative": 0}, "No analysis (missing API key)."

    joined = "\n".join(f"- {s}" for s in snippets)

    prompt = f"""
You are a highly experienced Forex trader with over 10 years of expertise in analyzing global currency markets. You’ve traded through rate hike cycles, QE tapers, geopolitical crises, and central bank pivots. Based on the input text (a set of recent news headlines and summaries), your job is to extract exactly FIVE high-impact market insights that are:

1. Highly relevant to currency traders and institutional investors.
2. Focused on macroeconomic developments, central bank signals, inflation trends, geopolitical risk, or surprise data points that could move FX markets.
3. Written in a consistent, narrative progression — from macro themes to specific trade implications.
4. Deeply analytical — avoid vague takeaways like “Fed to pause hikes” or “Euro may rise.” Each insight must include the **cause**, **effect**, and **market implication**.

Use a professional, precise, and insight-rich tone. Your goal is to provide **immediate trading value**.


Each takeaway should follow this structure:
- **Headline-style summary** (bold)
- 1-2 sentences of detailed analysis (include *why it matters*, *how it affects specific currencies*, and *what traders should watch next*)

---

**EXAMPLE OUTPUT:** PLEASE STICK TO THIS FORMAT AND DONOT GIVE ME 1 LINE Responses 

1. **Dollar faces fresh headwinds as Fed minutes signal a dovish pivot**  
   The FOMC minutes reveal growing consensus to hold off on further hikes amid cooling labor market data. This could suppress USD demand short-term, especially against yield-seeking pairs like AUD and NZD.

2. **Euro resilience supported by hawkish ECB tones despite growth concerns**  
   ECB board members maintain a data-dependent but hawkish stance, citing sticky core inflation. EUR/USD may find support unless upcoming PMI data disappoints significantly.

3. **JPY strengthens on safe haven flows amid Middle East tensions**  
   Renewed geopolitical risk is prompting risk-off sentiment globally, benefitting JPY and CHF. Traders should monitor oil price spikes and U.S. defense positioning for directional cues.

4. **Sterling under pressure as UK wage growth cools sharply**  
   Slower-than-expected wage data dampens BoEs tightening outlook. GBP/USD risks breaking below key support if CPI also moderates this week.

5. **Emerging market currencies vulnerable as US 10Y yield rebounds**  
   A sharp uptick in U.S. long-end yields is reversing recent capital flows to EMs. Currencies like INR, BRL, and ZAR could see renewed selling pressure, especially if U.S. retail sales surprise on the upside.

---

Now, based on the above 5 insights, do the following:

6. Assign an **overall sentiment** from one of the following five options:
   - Positive
   - Trending Positive
   - Neutral
   - Trending Negative
   - Negative

While providing the sentiment, don't just provide Neutral for everything, make it nuanced, try to understand the why behind the news and only
then give a proper sentiment score. It should not be just a blanket Neutral for everything. That is why the 5 options are very important. 
Try your best to put the snetiment in either of the 4 Positive, Trending Positive, Trending Negative, Negative. Only give Neutral if you truly are 
unsure about putting it in any of these 4 ones. So, all I am saying is that NEUTRAL SHOULD BE YOUR LAST OPTION, NOT THE FIRST THING YOU FEEL.    

7. Provide a 2-3 sentence **explanation** for this sentiment label. Make this grounded in the themes you identified. 
Don’t be vague — clearly connect it to central bank tone, risk sentiment, data, or market reactions. 
The user of the responses will be a Foreign Exchange trader at a bank so it should be detailed and you should be able to
give  a proper explanation to you responses. This is vital as it will be vital in allowing the users make decisions and understanding the 
moods and the sentiments of the market.  

8. Return a count of sentiment-bearing headlines, like:
   {{ "positive": X, "neutral": Y, "negative": Z }}

---

Respond only with a valid Python dictionary in this format:

{{
  "summary_points": [
    "Insight 1 (headline + explanation)",
    "Insight 2 (headline + explanation)",
    "Insight 3 (headline + explanation)",
    "Insight 4 (headline + explanation)",
    "Insight 5 (headline + explanation)"
  ],
  "overall_sentiment": "Positive | Trending Positive | Neutral | Trending Negative | Negative",
  "sentiment_explainer": "This week’s sentiment is [label] because ...",
  "counts": {{ "positive": X, "neutral": Y, "negative": Z }}
}}

Ignore any inputs that are unrelated to finance, macroeconomics, or global currency markets. 
If the input text contains irrelevant or off-topic content — such as science, pop culture, or general knowledge — 
respond only with:

"I'm trained to answer questions on Finance and Foreign Exchanges. That question is outside my scope."

Do not attempt to fabricate responses in unrelated areas. Stick strictly to financial and FX-related topics.

Here are the latest forex headlines:
{joined}
"""

    def _strip_code_fences(s: str) -> str:
        s = s.strip()
        if s.startswith("```"):
            # remove first fence line
            s = s.split("\n", 1)[1] if "\n" in s else ""
        if s.endswith("```"):
            s = s.rsplit("\n", 1)[0]
        return s.strip()

    try:
        resp = client.chat.completions.create(
            model="gpt-4.1-mini",
            messages=[
                {"role": "system", "content": "You are a highly specialized FX/macro assistant. Always reply with valid JSON."},
                {"role": "user", "content": prompt},
            ],
            temperature=0.0,
            max_tokens=1200,
        )

        content = (resp.choices[0].message.content or "").strip()
        if not content:
            st.error("OpenAI returned an empty response.")
            return [], "neutral", {"positive": 0, "neutral": 0, "negative": 0}, "No explanation (empty response)."

        # 1) remove code fences if present
        content = _strip_code_fences(content)

        # 2) try direct JSON parse
        try:
            result = json.loads(content)
        except json.JSONDecodeError:
            # 3) try to extract the first {...} block
            m = re.search(r"\{.*\}", content, flags=re.DOTALL)
            if not m:
                st.error("Could not parse GPT output (no JSON object found).")
                return [], "neutral", {"positive": 0, "neutral": 0, "negative": 0}, "No explanation (non-JSON response)."
            try:
                result = json.loads(m.group(0))
            except Exception as e:
                st.error(f"Could not parse GPT JSON: {e}")
                return [], "neutral", {"positive": 0, "neutral": 0, "negative": 0}, "No explanation (JSON parse error)."

        bullets = result.get("summary_points", []) or []
        tone = result.get("overall_sentiment", "neutral") or "neutral"
        explanation = result.get("sentiment_explainer", "No explanation provided.") or "No explanation provided."
        counts = result.get("counts", {}) or {}
        for k in ("positive", "neutral", "negative"):
            counts.setdefault(k, 0)

        return bullets, tone, counts, explanation

    except Exception as e:
        # e.g., auth/rate-limit/network errors
        st.error(f"GPT analysis failed: {e}")
        return [], "neutral", {"positive": 0, "neutral": 0, "negative": 0}, "No explanation (API error)."


# ── Renderer Functions (unchanged) ────────────────────────

from collections import defaultdict
from datetime import datetime, timedelta

 # See what’s coming from RSS


def render_week_ahead_horizontal(events: List[Dict]):
    st.markdown("---")
    st.markdown("### 📅 Week Ahead (Global Events)")

    days = ["Mon", "Tue", "Wed", "Thu", "Fri"]
    cols = st.columns(len(days))

    # Group events by weekday
    grouped = {day: [] for day in days}
    for ev in events:
        wd = ev.get("weekday", "")
        if wd in grouped:
            grouped[wd].append(f"**{ev['region']}:** {ev['event']}")

    # Render each column (day)
    for i, day in enumerate(days):
        with cols[i]:
            st.markdown(f"**{day}**")
            if grouped[day]:
                for item in grouped[day]:
                    st.markdown(f"- {item}")
            else:
                st.markdown("*No events*")





# ── Renderer: Sentiment Panels ───────────────────────────
# ── Helper: Assign color class to sentiment ──
def get_sentiment_class(sentiment: str) -> str:
    s = sentiment.lower().strip()
    if "negative" in s:
        return "background-color: #ff4d4d; color: white; padding: 6px 12px; border-radius: 6px; display: inline-block;"
    elif "positive" in s:
        return "background-color: #4CAF50; color: white; padding: 6px 12px; border-radius: 6px; display: inline-block;"
    else:
        return "background-color: #ffcc00; color: black; padding: 6px 12px; border-radius: 6px; display: inline-block;"


# ── Renderer: Global Panel ───────────────────────
def render_global_panel(bullets: List[str], overall: str, breakdown: Dict[str,int], explanation: str):
    st.markdown("### 🌍 Global FX Sentiment")

    sentiment_style = get_sentiment_class(overall)
    st.markdown(f"""<div style="{sentiment_style}; margin-bottom: 1rem;"><strong>Overall Sentiment:</strong> {overall}</div>""", unsafe_allow_html=True)

    with st.expander("Why this sentiment?"):
        st.markdown(clean_text(explanation))

    st.markdown(f"""<div class="card"><h3>Key Takeaways</h3></div>""", unsafe_allow_html=True)
    for i, b in enumerate(bullets, 1):
        b = clean_text(b)

        # Try to isolate **bold headline** from the rest
        match = re.match(r"^\*\*(.+?)\*\*(.*)", b)
        if match:
            headline = match.group(1).strip()
            explanation = match.group(2).strip()
            st.markdown(f"- **{headline}**  \n  {explanation}")
        else:
            # fallback in case there's no proper **headline**
            st.markdown(f"- {b}")


    fig = px.pie(names=list(breakdown.keys()), values=list(breakdown.values()), hole=0.4)
    fig.update_traces(textinfo="percent+label", marker=dict(line=dict(color="white", width=2)))
    fig.update_layout(margin=dict(l=0, r=0, t=0, b=0), paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)")
    st.plotly_chart(fig, use_container_width=True)


# ── Renderer: Currency Panel ─────────────────────
def render_currency_panel(bullets: List[str], overall: str, breakdown: Dict[str,int], explanation: str):
    st.markdown("### 💱 Currency-Pair Deep Dive")

    sentiment_style = get_sentiment_class(overall)
    st.markdown(f"""<div style="{sentiment_style}; margin-bottom: 1rem;"><strong>Overall Sentiment:</strong> {overall}</div>""", unsafe_allow_html=True)

    with st.expander("Why this sentiment?"):
        st.markdown(explanation)

    st.markdown(f"""<div class="card"><h3>Highlights</h3></div>""", unsafe_allow_html=True)
    for i, b in enumerate(bullets, 1):
        if "**" in b:
            headline, rest = b.split("**", 2)[1], b.split("**", 2)[2].strip()
            st.markdown(f"- **{headline}**  \n  {rest}")
        else:
            st.markdown(f"- {b}")

    fig = px.pie(names=list(breakdown.keys()), values=list(breakdown.values()), hole=0.4)
    fig.update_traces(textinfo="percent+label", marker=dict(line=dict(color="white", width=2)))
    fig.update_layout(margin=dict(l=0, r=0, t=0, b=0), paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)")
    st.plotly_chart(fig, use_container_width=True)

def render_news_links(news_items: List[Dict]):
    st.markdown("""
    <div style='font-size: 0.3rem; line-height: 1;'>
    """, unsafe_allow_html=True)

    for item in news_items:
        title = item.get("title", "")
        source = item.get("source", "")
        url = item.get("url", "#")

        st.markdown(
            f"- **[{title}]({url})** — *{source}*",
            unsafe_allow_html=True
        )

    st.markdown("</div>", unsafe_allow_html=True)

# ── Main App ──────────────────────────────────────────────
def main():
    if "chat_history" not in st.session_state:
        st.session_state.chat_history = []
    if "summary_ready" not in st.session_state:
        st.session_state.summary_ready = False

    allowed_sources = {
        "Bloomberg", "Reuters", "CNBC", "Financial Times", "Guardian",
        "Yahoo Finance", "Barrons", "BBC", "Forex Live", "FXStreet",
        "Market Watch", "Economist", "Nikkei", "Fed", "ECB",
        "Zero Hedge", "Yahoo"
    }

    st.title("TreasuryLens")
    st.subheader("Currency Market Insights")

    # ── GLOBAL FX SENTIMENT ─────────────────────
    if st.button("Fetch Global FX Sentiment"):
        with st.spinner("Fetching and analyzing global news..."):
            try:
                headlines, used_fallback = fetch_global_headlines(
                    desired_count=10,
                    max_offset=150,
                    allowed_sources=allowed_sources
                )
                snippets = [f"{h['title']} — {h['description']}" for h in headlines]
                bullets, overall, counts, explanation = analyze_with_gpt(snippets)

                if used_fallback:
                    explanation = "**⚠ Note:** Not enough trusted news sources (e.g., Bloomberg, Reuters) were found. Analysis is based on broader sources.\n\n" + explanation

                st.session_state["summary_data"] = {
                    "snippets_raw": headlines,
                    "snippets": snippets,
                    "bullets": bullets,
                    "overall": overall,
                    "counts": counts,
                    "explanation": explanation,
                }
                st.session_state["global_used_fallback"] = used_fallback
                st.session_state.summary_ready = True
                st.session_state.chat_history = []
            except Exception as e:
                st.error(f"Could not fetch and analyze global sentiment: {e}")
                st.session_state.summary_ready = False

    if st.session_state.summary_ready:
        bullets = st.session_state["summary_data"]["bullets"]
        overall = st.session_state["summary_data"]["overall"]
        counts = st.session_state["summary_data"]["counts"]
        explanation = st.session_state["summary_data"]["explanation"]
        headlines = st.session_state["summary_data"]["snippets_raw"]
        used_fallback = st.session_state.get("global_used_fallback", True)

        render_global_panel(bullets, overall, counts, explanation)

        with st.expander("📰 Headlines Used in Analysis"):
            st.markdown(f"**Articles used:** {len(headlines)} {'(Filtered)' if not used_fallback else '(Fallback - broader sources)'}")
            render_news_links(headlines)

        st.markdown("#### Ask a follow-up question")
        user_followup = st.text_input("Your question:", key="followup_input")

        if st.button("Submit Follow-Up"):
            if user_followup.strip():
                with st.spinner("Thinking..."):
                    st.session_state.chat_history.append({"role": "user", "content": user_followup})

                    if not any("summary_of_sentiment" in m.get("name", "") for m in st.session_state.chat_history):
                        summary_context = "\n".join(f"- {pt}" for pt in bullets)
                        st.session_state.chat_history.insert(0, {
                            "role": "user",
                            "content": f"Summary of recent FX sentiment:\n{summary_context}",
                            "name": "summary_of_sentiment"
                        })

                    system_msg = {
                        "role": "system",
                        "content": "You are a helpful FX market assistant. Be concise, insightful, and use macro/FX terminology when relevant."
                    }

                    messages = [system_msg] + [
                        {k: v for k, v in m.items() if k in ["role", "content"]} for m in st.session_state.chat_history
                    ]

                    try:
                        response = client.chat.completions.create(
                            model="gpt-4.1-mini",
                            messages=messages,
                            temperature=0.4,
                        )
                        reply = response.choices[0].message.content.strip()
                        st.session_state.chat_history.append({"role": "assistant", "content": reply})
                    except Exception as e:
                        st.error(f"Follow-up failed: {e}")

        if st.session_state.chat_history:
            st.markdown("---")
            st.markdown("#### Conversation History")
            for msg in st.session_state.chat_history:
                if msg["role"] == "user":
                    st.markdown(f"**You:** {msg['content']}")
                elif msg["role"] == "assistant":
                    st.markdown(f"**GPT:** {msg['content']}")

        if st.button("Clear Chat History"):
            st.session_state.chat_history = []
            st.experimental_rerun()

    st.markdown("---")


    # ── CURRENCY PAIR SENTIMENT ──────────────────
    pair = st.selectbox("Select Currency Pair to Analyze:", [
        "EUR/USD", "EUR/GBP", "USD/GBP", "EUR/JPY", "EUR/AUD",
        "EUR/CAD", "EUR/INR", "USD/CNH", "EUR/CHF", "EUR/NOK", 
        "EUR/NZD"
    ])

    if st.button("Analyze This Pair"):
        with st.spinner(f"Analyzing sentiment for {pair}..."):
            try:
                headlines, used_fallback = fetch_currency_headlines(
                    pair=pair,
                    desired_count=10,
                    max_offset=150,
                    allowed_sources=allowed_sources
                )

                # after fetch_global_headlines(...)
                st.caption(f"Fetched {len(headlines)} headlines")
                snippets = [f"{h['title']} — {h['description']}" for h in headlines]
                bullets, overall, counts, explanation = analyze_with_gpt(snippets)

                if used_fallback:
                    explanation = f"**⚠ Note:** Not enough trusted news sources for {pair}. Using broader news coverage instead.\n\n" + explanation

                st.session_state["currency_headlines"] = headlines
                st.session_state["currency_used_fallback"] = used_fallback

                render_currency_panel(bullets, overall, counts, explanation)

            except Exception as e:
                st.error(f"Could not fetch or analyze {pair}: {e}")

    if "currency_headlines" in st.session_state:
        with st.expander(f"📰 News Sources for {pair}"):
            headlines = st.session_state["currency_headlines"]
            used_fallback = st.session_state.get("currency_used_fallback", True)
            st.markdown(f"**Articles used:** {len(headlines)} {'(Filtered News Sources Only)' if not used_fallback else '(Fallback - Broader New Sources)'}")
            render_news_links(headlines)

    # ── MACRO CALENDAR ───────────────────────────
    events = scrape_calendar()
    render_week_ahead_horizontal(events)





if __name__ == "__main__":
    main()
