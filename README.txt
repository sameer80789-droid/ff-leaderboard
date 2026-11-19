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
