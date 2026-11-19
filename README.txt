HOST THIS PROJECT ON RENDER

1) Upload these files/folders to a PRIVATE GitHub repository.
2) On Render, choose New > Blueprint and connect the repository.
3) Render will read render.yaml and create the web service.
4) When prompted for FF_ACCOUNTS_JSON, paste a JSON array of account objects.
   Example shape: [{"uid":"YOUR_UID","password":"YOUR_PASSWORD","region":"PK"}]
   Replace with your actual account values. Keep this repository PRIVATE.
5) Deploy. Open the generated *.onrender.com URL.

Free Render web services sleep after 15 minutes without traffic and may take about
a minute to wake up. This is not 24/7 always-on hosting.

----- IF A REGION SHOWS "MajorLogin failed" -----
Open  https://YOUR-APP.onrender.com/healthz  -> "login_errors" shows what Garena actually answered
for each region, and "cooldowns" shows when the next try happens (30s, 60s, 120s ... max 10 min).
Also check the Render logs for lines starting with "MajorLogin <REGION>:".

Tips:
 - Do NOT run the same accounts on your PC while Render is running. Two servers logging into the same
   guest account at the same time can make Garena refuse one of them.
 - Give each region 2+ accounts in FF_ACCOUNTS_JSON; a failed login automatically tries the next one.
 - Env FF_LOGIN_GAP (default 2.5) = seconds between any two logins. Raise it if Garena still refuses.
