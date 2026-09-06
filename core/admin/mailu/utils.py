@@
 def formatCSVField(field):
     if not field.data:
         field.data = ''
         return
     if isinstance(field.data,str):
         data = field.data.replace(" ","").split(",")
     else:
         data = field.data
     field.data = ", ".join(data)
+
+
+def notify_fetchmail_reload(fetch_ids):
+    """
+    Notify fetchmail controller to reload one or more fetch configs.
+    - fetch_ids: an int, list/tuple of ints, or empty/None for full reconcile.
+    Env:
+      FETCHMAIL_WEBHOOK_URL (default: http://fetchmail:8888/internal/fetch/reload)
+      FETCHMAIL_WEBHOOK_SECRET (optional) -> sent as X-FETCHMAIL-SECRET
+    """
+    import os
+    import requests
+    if not fetch_ids:
+        payload = {}
+    else:
+        if isinstance(fetch_ids, (list, tuple, set)):
+            payload = list(fetch_ids)
+        else:
+            try:
+                payload = {"id": int(fetch_ids)}
+            except Exception:
+                payload = {"id": fetch_ids}
+    url = os.environ.get("FETCHMAIL_WEBHOOK_URL", "http://fetchmail:8888/internal/fetch/reload")
+    headers = {"Content-Type": "application/json"}
+    secret = os.environ.get("FETCHMAIL_WEBHOOK_SECRET")
+    if secret:
+        headers["X-FETCHMAIL-SECRET"] = secret
+    try:
+        requests.post(url, json=payload, headers=headers, timeout=2)
+    except Exception:
+        try:
+            app.logger.exception("Failed to notify fetchmail webhook")
+        except Exception:
+            print("Failed to notify fetchmail webhook")
