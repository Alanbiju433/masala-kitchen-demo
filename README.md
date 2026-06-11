# 🍛 Masala Kitchen — AI Chatbot Demo

A full restaurant website with an embedded GPT-4o-powered AI chatbot.
Built as a portfolio demo by **Alan Biju** to showcase AI chatbot development for small businesses.

**Live demo:** [your-render-url.onrender.com]

---

## What it does

- Full single-page restaurant website (hero, deals, menu, about, contact)
- Floating AI chat bubble (bottom-right)
- GPT-4o chatbot that knows the full menu, allergens, opening hours, deals, and booking flow
- Booking assistant — collects name, date, time, party size, and phone number
- Quick-reply buttons for common questions
- Conversation history per session

---

## Tech stack

| Layer | Technology |
|-------|-----------|
| Backend | Python / Flask |
| AI | OpenAI GPT-4o-mini |
| Frontend | HTML + CSS + Vanilla JS |
| Deployment | Render.com |

---

## Local setup

```bash
git clone https://github.com/Alanbiju433/masala-kitchen-demo.git
cd masala-kitchen-demo
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env  # add your OPENAI_API_KEY
python app.py
```

Open http://localhost:5000

---

## Deploy to Render (free tier)

1. Push this repo to GitHub
2. Go to render.com → New → Web Service
3. Connect this repo
4. Build command: `pip install -r requirements.txt`
5. Start command: `gunicorn app:app`
6. Add env vars: OPENAI_API_KEY and SECRET_KEY
7. Deploy — live in ~2 minutes

---

## Customising for a real client

All data is in `data/restaurant.json`. Swap the name, menu, hours, deals — the chatbot learns the new data automatically on startup. No code changes needed.

---

## Built by

**Alan Biju** — CS student at University of Northampton
GitHub: github.com/Alanbiju433
