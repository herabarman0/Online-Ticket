# লাকি মেলা (Flask)
Run:     pip install -r requirements.txt && python app.py
Render:  Build `pip install -r requirements.txt`  Start `gunicorn app:app`
Env:     SECRET_KEY (required), GOOGLE_CLIENT_ID (Gmail login), DB_PATH (e.g. /data/lucky.db on a Render Disk)
Google:  Cloud Console > Credentials > OAuth client ID (Web); add your site URL to Authorized JavaScript origins.
Ads:     /ad is a placeholder reward endpoint. Before launch, connect AdMob/Unity server-side reward callbacks.
Note:    Render free disk is wiped on redeploy; attach a Disk or use Postgres.
