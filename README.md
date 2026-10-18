# MutualTrack

Mutual Fund & SIP tracking system for a college DBMS project.

The UI is based on the existing MutualTrack HTML prototype (dark navy sidebar, orange accent, white cards). Sample values are demonstration data only — NAVs are not live market prices.

## Stack

- Frontend: HTML, CSS, JavaScript, Chart.js
- Backend: Python, Flask
- Database: MySQL

## Setup

1. Install Python 3.10+ and MySQL 8+.

2. Create a virtual environment and install packages:

```bash
pip install -r requirements.txt
```

3. Copy environment variables and set your MySQL password:

```bash
copy .env.example .env
```

Edit `.env`:

```
DB_HOST=localhost
DB_PORT=3306
DB_USER=root
DB_PASSWORD=your_password
DB_NAME=mutualtrack
SECRET_KEY=change-this-to-a-random-string
```

4. Create tables and load demo data:

```bash
python setup_db.py
```

5. Run the app:

```bash
python app.py
```

Open [http://localhost:5000](http://localhost:5000).

## Demo data

The seed file includes 3 investors, 5 AMCs, 12 schemes, SIPs, goals, NAV history, and 20+ transactions. Portfolio holdings are rebuilt from those transactions.

Use the top-bar dropdown to switch investors. Default profile: **Aditya Sharma**.

## Project layout

```
app.py
config.py
db.py
logic.py
setup_db.py
database/schema.sql
database/seed.sql
templates/
static/css/style.css
static/js/app.js
```
