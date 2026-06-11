import os
import json
from flask import Flask, render_template, request, jsonify, session
from openai import OpenAI

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "masala-kitchen-dev-key-change-in-prod")

client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))

# Load restaurant data once at startup
with open("data/restaurant.json", "r") as f:
    RESTAURANT = json.load(f)

# Build a rich system prompt from the restaurant data
def build_system_prompt():
    r = RESTAURANT
    menu_text = ""
    for category, items in r["menu"].items():
        menu_text += f"\n{category}:\n"
        for item in items:
            vegan_tag = " [VEGAN]" if item["vegan"] else ""
            allergen_tag = f" (allergens: {', '.join(item['allergens'])})" if item["allergens"] else ""
            menu_text += f"  - {item['name']} — £{item['price']:.2f}: {item['description']}{vegan_tag}{allergen_tag}\n"

    hours_text = "\n".join([f"  {day}: {time}" for day, time in r["hours"].items()])
    deals_text = "\n".join([f"  - {d['name']}: {d['description']} ({d['valid']})" for d in r["deals"]])
    faqs_text = "\n".join([f"  Q: {f['q']}\n  A: {f['a']}" for f in r["faqs"]])

    return f"""You are the friendly AI assistant for {r['name']} — "{r['tagline']}".

You help customers with menu questions, allergen info, bookings, deals, opening hours, and general enquiries. You're warm, helpful, and knowledgeable. Keep responses concise and conversational — like a helpful member of staff, not a wall of text.

== RESTAURANT INFO ==
Name: {r['name']}
Address: {r['address']}
Phone: {r['phone']}
Email: {r['email']}
Services: {', '.join(r['services'])}

== OPENING HOURS ==
{hours_text}

== CURRENT DEALS & OFFERS ==
{deals_text}

== FULL MENU ==
{menu_text}

== BOOKING INFO ==
{r['booking_info']['how']}
Advance booking: up to {r['booking_info']['max_advance']}
Large groups (8+): {r['booking_info']['large_groups']}
Confirmation: {r['booking_info']['note']}

When a customer wants to make a booking, collect: name, date, time, party size, and a contact phone number. Once you have all 5, confirm the booking details back to them and say the team will call to confirm within 2 hours.

== FAQs ==
{faqs_text}

== RULES ==
- If asked about something not on the menu or not related to the restaurant, politely say you can only help with Masala Kitchen questions.
- Never make up prices, dishes, or information not listed above.
- Always mention allergens when a customer asks about specific dietary requirements.
- For complaints or complex issues, ask the customer to call {r['phone']} or email {r['email']}.
- Be conversational and warm. Use occasional friendly phrases but don't overdo it.
"""

SYSTEM_PROMPT = build_system_prompt()


@app.route("/")
def index():
    return render_template("index.html", restaurant=RESTAURANT)


@app.route("/chat", methods=["POST"])
def chat():
    data = request.get_json()
    user_message = data.get("message", "").strip()

    if not user_message:
        return jsonify({"error": "No message provided"}), 400

    if "history" not in session:
        session["history"] = []

    session["history"].append({"role": "user", "content": user_message})
    history = session["history"][-20:]

    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "system", "content": SYSTEM_PROMPT}] + history,
            max_tokens=400,
            temperature=0.7,
        )
        assistant_message = response.choices[0].message.content
        session["history"].append({"role": "assistant", "content": assistant_message})
        session.modified = True
        return jsonify({"reply": assistant_message})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/reset", methods=["POST"])
def reset():
    session.pop("history", None)
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    app.run(debug=True, port=5000)
