# -*- coding: utf-8 -*-
"""VPN 网页模式离线测试：用本地 mock 网关模拟深信服接口，无需真实账号。

运行：python tests/test_vpn_flow.py
"""
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import urp_auto  # noqa: E402

MAPID = "testmap1"
RESOURCE_XML = """<?xml version="1.0" encoding="UTF-8"?>
<RESPONSE>
  <RESOURCE_LIST>
    <WEB_RESOURCE mapid="other999" uri="L290dGhlci8=" name="其它系统" status="1" authority="1" display="1"/>
    <WEB_RESOURCE mapid="%s" uri="Lw==" name="教务系统1" status="1" authority="1" display="1"/>
  </RESOURCE_LIST>
</RESPONSE>""" % MAPID

URP_LOGIN = """<html><head><title>URP综合教务系统 - 登录</title></head>
<body><form name="loginForm" action="/loginAction.do"><input name="zjh"></form></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, code, body=b"", ctype="text/html; charset=GBK", headers=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _cookies(self):
        raw = self.headers.get("Cookie", "")
        return dict(p.strip().split("=", 1) for p in raw.split(";") if "=" in p)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/vpn/theme/auth_home.html":
            return self._send(200, b"<html>login</html>", headers={
                "Set-Cookie": "VSG_VERIFYCODE_CONF=0-0; Path=/"})
        if path == "/vpn/user/portal/intergration.xml":
            if self._cookies().get("VSG_SESSIONID") != "sess-ok":
                return self._send(200, b"<html>login</html>")
            return self._send(200, RESOURCE_XML.encode("utf-8"),
                              ctype="text/xml; charset=UTF-8")
        if path == "/%s/" % MAPID:
            return self._send(200, URP_LOGIN.encode("gbk"))
        if path == "/%s/loginAction.do" % MAPID:
            return self._send(200, URP_LOGIN.encode("gbk"))
        if path == "/%s/jxpgXsAction.do" % MAPID:
            return self._send(200, b"<html><body>JXPGLIST-OK</body></html>")
        if path == "/%s/redirect.do" % MAPID:
            return self._send(302, b"", headers={"Location": "/%s/next.do" % MAPID})
        if path == "/%s/abs-redirect.do" % MAPID:
            return self._send(302, b"", headers={
                "Location": "http://192.168.16.207/abs-next.do"})
        if path in ("/%s/next.do" % MAPID, "/%s/abs-next.do" % MAPID):
            return self._send(200, b"NEXT-OK")
        return self._send(404, b"not found")

    def do_POST(self):
        path = self.path.split("?")[0]
        n = int(self.headers.get("Content-Length", 0))
        data = parse_qs(self.rfile.read(n).decode())
        if path == "/vpn/user/auth/password":
            user = data.get("username", [""])[0]
            pwd = data.get("password", [""])[0]
            if user == "2415" and pwd == "vpnpwd":
                body = (b'<?xml version="1.0"?><RESPONSE><RESULT code="0" msg="ok" '
                        b'sid_name="VSG_SESSIONID"/></RESPONSE>')
                return self._send(200, body, ctype="text/xml; charset=UTF-8",
                                  headers={"Set-Cookie": "VSG_SESSIONID=sess-ok; Path=/"})
            body = (b'<?xml version="1.0"?><RESPONSE><RESULT code="0x8000001A" '
                    b'msg="username or password error"/></RESPONSE>')
            return self._send(200, body, ctype="text/xml; charset=UTF-8",
                              headers={"Set-Cookie": "VSG_SESSIONID=; Path=/"})
        return self._send(404)


def main():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    gateway = "http://127.0.0.1:%d" % port
    print("mock gateway:", gateway)

    vpn = urp_auto.SangforWebVPN(gateway, log=print)

    try:
        vpn.login("2415", "wrong")
        raise AssertionError("错误密码不应登录成功")
    except urp_auto.VPNError:
        print("OK  错误密码被拒绝")

    vpn.login("2415", "vpnpwd")
    print("OK  登录成功")

    base, res = vpn.find_urp_base()
    assert base == gateway + "/" + MAPID + "/", base
    print("OK  定位资源:", res["name"], "->", base)

    client = urp_auto.URPClient(base_url=base.rstrip("/"), log=print, session=vpn.s)
    html = client._text_gbk(client._get("/jxpgXsAction.do?oper=listWj&yzxh=1"))
    assert "JXPGLIST-OK" in html
    print("OK  URP 请求走 VPN 前缀")

    r = client._get("/redirect.do")
    assert r.text.strip() == "NEXT-OK"
    print("OK  站根重定向补前缀")

    r = client._get("/abs-redirect.do")
    assert r.text.strip() == "NEXT-OK"
    print("OK  内网绝对地址重定向改写")

    srv.shutdown()
    print("\nALL PASS")


if __name__ == "__main__":
    main()
