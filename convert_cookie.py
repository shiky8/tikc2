import json

def netscape_to_json(netscape_file, output_file):
    cookies = []
    with open(netscape_file, "r", encoding="utf-8") as f:
        for line in f:
            if line.startswith("#") or not line.strip():
                continue
            parts = line.strip().split("\t")
            if len(parts) < 7:
                continue
            domain, flag, path, secure, expiration, name, value = parts
            cookie = {
                "domain": domain,
                "path": path,
                "secure": secure.upper() == "TRUE",
                "httpOnly": False,  # not in Netscape format, assume False
                "sameSite": "Lax",  # TikTok usually defaults this way
                "expires": int(expiration) if expiration.isdigit() else -1,
                "name": name,
                "value": value,
            }
            cookies.append(cookie)

    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(cookies, f, indent=2)
    print(f"✅ Converted {netscape_file} → {output_file}")

# Usage:
netscape_to_json("cookie3.txt", "cookies.json")
