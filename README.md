# 🏪 VyaparAI — Smart Kirana Restocking Assistant

[![FastAPI](https://img.shields.io/badge/FastAPI-0.115+-009688.svg?style=flat&logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com)
[![Python](https://img.shields.io/badge/Python-3.10+-3776AB.svg?style=flat&logo=python&logoColor=white)](https://www.python.org/)
[![UI Style](https://img.shields.io/badge/Design-macOS%20Style-007AFF.svg?style=flat&logo=apple&logoColor=white)](#design--interface)
[![Deploy with Vercel](https://vercel.com/button)](https://vercel.com/new)

**VyaparAI** is a smart inventory management and restocking assistant tailored for Indian Kirana and retail store owners. Designed with an elegant, modern **Apple macOS-inspired interface**, it helps store owners optimize their working capital, prevent stockouts, compare supplier deals, draft instant WhatsApp orders, and answer natural language budget queries in Hinglish.

---

## ✨ Features

### 🖥️ Apple macOS-Inspired Interface
- **Desktop Window Chrome**: Traffic light buttons, titlebar with live status indicator, and structured sidebar layout.
- **Glassmorphic Aesthetic**: Translucent sidebar, subtle borders, backdrop blur, clean SF-style typography, and responsive grid layouts.
- **Visual Stock Health**: Color-coded badges for stock status:
  - 🔴 `Restock soon` (Stock will run out before supplier delivery)
  - 🟢 `Enough stock until delivery`
  - ⚪ `No recent sales`

### 💰 Smart Budget-Based Order Planner
- Automatically analyzes daily sales velocity and supplier lead times.
- Calculates exact **stockout gap days** and prioritizes items facing imminent stockouts.
- Efficiently allocates available working capital (e.g. ₹600, ₹1,000) to maximize stock coverage and prevent lost sales.
- Displays remaining cash and exact pack breakdown.

### 🤖 "Ask VyaparAI" Hinglish AI Assistant
- Conversational query parser supporting natural Hindi, English, and Hinglish inputs:
  - *"Mere paas ₹600 hain, kya mangwau?"*
  - *"Budget ₹1200 hai, konsa saman order karu?"*
  - *"I have 800 rupees, plan my order"*
- Automatically extracts the budget amount, executes the planner, and provides a clear conversational explanation in Hinglish.

### 📱 1-Click WhatsApp Supplier Order Draft
- Automatically formats clean, polite supplier messages:
  - *"Namaste, please send 6 packs of Atta. Please confirm availability and delivery date."*
- Features a **Copy Message** button with animated clipboard feedback.
- Features an **Open WhatsApp** button (`https://wa.me/?text=...`) for one-tap review and dispatch.

### ⚖️ Supplier Comparison (Oil Trade-off Analysis)
- Side-by-side trade-off analysis comparing suppliers:
  - **Regular Supplier**: Lower cost (₹150/pack), longer lead time (4 days), potential stock gap.
  - **Faster Supplier**: Premium cost (₹170/pack), next-day delivery (1 day), zero stock gap.
- Live calculations for packs affordable, total cost, remaining balance, and stockout gap.
- Dedicated "Use this option for Oil" buttons to generate instant single-product drafts.

### 📊 CSV Import & Sample Template Export
- **Download Sample CSV**: 5-column template (`product`, `packs_in_stock`, `packs_sold_per_day`, `supplier_delivery_days`, `wholesale_cost_per_pack`).
- **Upload Inventory CSV**: Instant validation with row-and-column error highlighting for invalid entries or missing columns.

---

## 📁 Project Structure

```text
vyapar_ai/
├── api/
│   └── index.py            # Vercel serverless entrypoint
├── main.py                 # Core FastAPI application (routing, business logic, UI)
├── requirements.txt        # Production dependencies (Fastapi, Uvicorn)
├── vercel.json             # Vercel serverless rewrite configuration
├── .gitignore              # Git ignore rules
└── README.md               # Documentation and setup instructions
```

---

## 🚀 Local Setup & Installation

### Prerequisites
- **Python 3.10+** installed on your system.
- **Git** installed.

### 1. Clone the Repository
```bash
git clone https://github.com/SidakSethi-Singh/vyapar_ai.git
cd vyapar_ai
```

### 2. Create and Activate a Virtual Environment
- **On Windows (PowerShell):**
  ```powershell
  python -m venv .venv
  .venv\Scripts\Activate.ps1
  ```
- **On macOS / Linux:**
  ```bash
  python3 -m venv .venv
  source .venv/bin/activate
  ```

### 3. Install Dependencies
```bash
pip install -r requirements.txt
```

### 4. Run the Development Server
```bash
uvicorn main:app --reload
```

The application will start at:
👉 **`http://127.0.0.1:8000`**

Open your browser and navigate to the URL to interact with VyaparAI.

---

## ☁️ Deploying to Vercel

This repository is pre-configured for seamless deployment to **Vercel** via serverless Python functions (`api/index.py` + `vercel.json`).

### Method 1: Deploy via Vercel Dashboard (Recommended)

1. Push your code to GitHub:
   ```bash
   git push origin main
   ```
2. Log in to [Vercel](https://vercel.com).
3. Click **"Add New..."** → **"Project"**.
4. Import the **`vyapar_ai`** repository.
5. In **Build and Output Settings**, keep the default settings (Vercel automatically detects the Python configuration).
6. Click **"Deploy"**.
7. Once deployed, Vercel will provide you with a live production URL (e.g., `https://vyapar-ai.vercel.app`).

### Method 2: Deploy using Vercel CLI

```bash
# Install Vercel CLI if you haven't already
npm install -g vercel

# Deploy to preview
vercel

# Deploy to production
vercel --prod
```

---

## 🛠️ Tech Stack & Architecture

- **Backend**: [FastAPI](https://fastapi.tiangolo.com/) (High-performance ASGI framework)
- **Server**: [Uvicorn](https://www.uvicorn.org/) (Lightning-fast ASGI server)
- **Deployment**: [Vercel Serverless Functions](https://vercel.com/docs/functions/serverless-functions/runtimes/python)
- **Zero Heavy Frontend Build**: Pure semantic HTML5, modern CSS3 (Flexbox/Grid, backdrop filters, CSS custom properties), and vanilla JavaScript. Fast loading and low memory footprint.

---

## 📄 License

This project is open-source and available under the [MIT License](LICENSE).
