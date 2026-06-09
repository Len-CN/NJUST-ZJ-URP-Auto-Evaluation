# -*- coding: utf-8 -*-
"""
紫金 URP 教务系统 自动评估软件
=================================
功能：自动登录 -> 拉取未评课程 -> 逐门自动选择最优选项 + 自动填写主观评价 -> 提交

适配站点结构（依据真实页面源码）：
  服务器        http://192.168.16.207
  登录          POST /loginAction.do   字段 zjh(学号) / mm(密码,明文) / v_yzm(验证码)
  验证码        GET  /validateCodeAction.do?random=...
  问卷列表      GET  /jxpgXsAction.do?oper=listWj&yzxh=学号
  打开问卷      POST /jxpgXsAction.do   oper=wjShow + wjbm/bpr/bprm/wjmc/pgnrm/pgnr + wjbz=null
  提交评估      POST /jxpgXsAction.do?oper=wjpg  wjbm/bpr/pgnr + xumanyzg=zg + wjbz + zgpj + 各题=值

编码：全站 GBK。所有请求体用 GBK 百分号编码，所有响应用 GBK 解码（关键！）。

选项策略：每题取 id=1（"非常同意"，分值最高 _1）。
防作弊：页面 JS check() 用 max(选中id)-min(选中id)==0 拦截"全部一样"。
        直接 POST 虽可绕过客户端 JS，但为防服务端同样校验，默认把最后 1 题改选
        id=2（"同意" _0.8），对教师总分影响极小，却能稳过任何"同答案"校验。
"""

import sys
import re
import time
import random
import threading
import queue
import urllib.parse

try:
    import requests
except ImportError:
    print("缺少依赖 requests，请先运行: pip install requests")
    raise

# ------- 可选依赖：验证码自动识别 -------
try:
    import ddddocr  # noqa
    _OCR = ddddocr.DdddOcr(show_ad=False)
    HAS_OCR = True
except Exception:
    _OCR = None
    HAS_OCR = False

# ------- 可选依赖：在界面里显示验证码图片 -------
try:
    from PIL import Image, ImageTk  # noqa
    HAS_PIL = True
except Exception:
    HAS_PIL = False


DEFAULT_SERVER = "http://192.168.16.207"

# 主观评价模板（均 < 100 字，符合 zgpj 长度限制；逐课轮换，避免完全雷同）
SUBJECTIVE_TEMPLATES = [
    "老师授课认真负责，内容充实，讲解清晰易懂，注重课堂互动，关心学生成长，让我受益匪浅，感谢老师的辛勤付出！",
    "老师教学经验丰富，重点突出，循循善诱，能调动课堂气氛，对学生耐心负责，这门课让我收获很大，非常感谢！",
    "老师备课充分，思路清晰，理论联系实际，善于启发思考，为人师表、治学严谨，希望以后还能上老师的课！",
    "老师讲课生动有条理，深入浅出，关注每位同学，认真解答疑问，既严格又亲切，这学期跟着老师学到了很多。",
    "老师专业功底扎实，授课深入浅出，注重培养我们的能力，课堂氛围轻松，受益良多，向老师道一声辛苦了！",
]


def log_noop(msg):
    print(msg)


class URPError(Exception):
    pass


class URPClient:
    """与 URP 服务器交互的核心客户端（不含界面）。"""

    def __init__(self, base_url=DEFAULT_SERVER, log=log_noop):
        self.base = base_url.rstrip("/")
        self.log = log
        self.s = requests.Session()
        self.s.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/120.0 Safari/537.36",
        })
        self.zjh = ""

    # ---------- 基础工具 ----------
    def _url(self, path):
        if path.startswith("http"):
            return path
        return self.base + path

    def _get(self, path, **kw):
        return self.s.get(self._url(path), timeout=30, **kw)

    def _post_gbk(self, path, pairs):
        """以 GBK 百分号编码提交表单，pairs 为 (key, value) 列表以保持顺序。"""
        body = urllib.parse.urlencode(pairs, encoding="gbk")
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        return self.s.post(self._url(path), data=body, headers=headers,
                           timeout=30, allow_redirects=True)

    @staticmethod
    def _text_gbk(resp):
        """统一用 GBK 解码响应，避免 requests 猜错编码导致乱码。"""
        return resp.content.decode("gbk", errors="replace")

    # ---------- 验证码 ----------
    def fetch_captcha(self):
        """返回验证码图片字节。"""
        r = self._get("/validateCodeAction.do?random=%s" % random.random())
        return r.content

    def recognize_captcha(self, img_bytes):
        """用 ddddocr 识别；无该库时返回空串。"""
        if not HAS_OCR:
            return ""
        try:
            code = _OCR.classification(img_bytes)
            return re.sub(r"[^0-9a-zA-Z]", "", code).strip()
        except Exception as e:
            self.log("验证码识别异常: %s" % e)
            return ""

    # ---------- 登录 ----------
    def login(self, zjh, mm, yzm):
        """提交登录。返回 True 表示成功。"""
        self.zjh = zjh.strip()
        pairs = [
            ("zjh1", ""), ("tips", ""), ("lx", ""),
            ("evalue", ""), ("eflag", ""), ("fs", ""), ("dzslh", ""),
            ("zjh", self.zjh),
            ("mm", mm),
            ("v_yzm", yzm.strip()),
        ]
        r = self._post_gbk("/loginAction.do", pairs)
        html = self._text_gbk(r)
        # 失败特征
        if "密码不正确" in html or "帐号不存在" in html or "账号不存在" in html:
            raise URPError("学号或密码错误")
        if "验证码" in html and ("错误" in html or "不正确" in html or "无效" in html):
            raise URPError("验证码错误")
        # 通过能否打开问卷列表来确认是否真正登录成功
        if self._logged_in():
            return True
        # 兜底：登录页仍在 => 失败（多半验证码错）
        if 'name="loginForm"' in html or "请输入您的帐号" in html:
            raise URPError("登录失败（验证码或账号密码不正确）")
        # 其它情况再验证一次
        return self._logged_in()

    def _logged_in(self):
        try:
            html = self.get_list_html()
        except Exception:
            return False
        return ('name="WjList"' in html) or ("学生评估问卷列表" in html)

    # ---------- 问卷列表 ----------
    def get_list_html(self):
        r = self._get("/jxpgXsAction.do?oper=listWj&yzxh=%s" %
                      urllib.parse.quote(self.zjh))
        return self._text_gbk(r)

    def get_courses(self):
        """解析未评估课程列表。返回 dict 列表。

        列表里每门课对应一个 img，name 形如：
          wjbm#@bpr#@bprm#@wjmc#@pgnrm#@pgnr
        onclick="evaluation(this)" 的才是"未评估/可评估"项。
        """
        html = self.get_list_html()
        courses = []
        # 只取 onclick=evaluation 的 img（即未评估项）
        pattern = re.compile(
            r'<img\s+name="([^"]*#@[^"]*)"[^>]*onclick="evaluation\(this\)"',
            re.I)
        for m in pattern.finditer(html):
            parts = m.group(1).split("#@")
            if len(parts) < 6:
                continue
            courses.append({
                "wjbm": parts[0], "bpr": parts[1], "bprm": parts[2],
                "wjmc": parts[3], "pgnrm": parts[4], "pgnr": parts[5],
            })
        return courses

    # ---------- 打开问卷（wjShow） ----------
    def open_questionnaire(self, c):
        pairs = [
            ("wjbm", c["wjbm"]), ("bpr", c["bpr"]), ("pgnr", c["pgnr"]),
            ("oper", "wjShow"), ("wjmc", c["wjmc"]),
            ("bprm", c["bprm"]), ("pgnrm", c["pgnrm"]),
            ("wjbz", "null"),
        ]
        r = self._post_gbk("/jxpgXsAction.do", pairs)
        return self._text_gbk(r)

    # ---------- 解析题目 ----------
    @staticmethod
    def parse_form(html):
        """从问卷页面解析隐藏字段与题目。

        返回 (hiddens dict, questions list)
        questions = [ {"name": "0000000138",
                       "options": [(id_int, value_str), ...]}, ... ] 顺序保留
        """
        hiddens = {}
        for m in re.finditer(
                r'<input\s+name="(wjbm|bpr|pgnr|xumanyzg|wjbz)"\s+type="hidden"\s+value="([^"]*)"',
                html, re.I):
            hiddens[m.group(1)] = m.group(2)

        questions = []
        index = {}
        for m in re.finditer(
                r'<input\s+type="radio"\s+id="(\d+)"\s+name="(\d+)"\s+value="([^"]+)"',
                html, re.I):
            rid = int(m.group(1))
            name = m.group(2)
            value = m.group(3)
            if name not in index:
                q = {"name": name, "options": []}
                index[name] = q
                questions.append(q)
            index[name]["options"].append((rid, value))
        return hiddens, questions

    @staticmethod
    def build_answers(questions, keep_one_second_best=True):
        """为每题选最优(id=1)；默认把最后一题改为次优(id=2)以规避"同答案"校验。

        返回 [(question_name, value), ...] 顺序与题目一致。
        """
        answers = []
        n = len(questions)
        for i, q in enumerate(questions):
            opts = sorted(q["options"], key=lambda o: o[0])  # 按 id 升序
            best = next((v for rid, v in opts if rid == 1), opts[0][1])
            # 最后一题（且总题数>1）选次优
            if keep_one_second_best and n > 1 and i == n - 1:
                second = next((v for rid, v in opts if rid == 2), best)
                answers.append((q["name"], second))
            else:
                answers.append((q["name"], best))
        return answers

    @staticmethod
    def _calc_xumanyzg(html):
        """复刻页面 JS：xumanyzg = "zg" + 前(n-1)个 textarea 的 name（逗号分隔）。
        当前问卷只有 zgpj 一个 textarea 时结果即 "zg"。"""
        names = re.findall(r'<textarea[^>]*\bname="([^"]+)"', html, re.I)
        s = "zg"
        for nm in names[:-1]:   # 排除最后一个（主观评价框本身）
            s += nm + ","
        if len(s) > 2:
            s = s[:-1]
        return s

    # ---------- 提交（wjpg） ----------
    def submit(self, html, zgpj_text, keep_one_second_best=True):
        hiddens, questions = self.parse_form(html)
        if not questions:
            raise URPError("未能解析到题目，页面结构可能已变化")
        answers = self.build_answers(questions, keep_one_second_best)

        # 按浏览器 DOM 顺序拼装：wjbm,bpr,pgnr,xumanyzg,wjbz,<各题>,zgpj
        pairs = [
            ("wjbm", hiddens.get("wjbm", "")),
            ("bpr", hiddens.get("bpr", "")),
            ("pgnr", hiddens.get("pgnr", "")),
            ("xumanyzg", self._calc_xumanyzg(html)),  # 复刻 JS 算法
            ("wjbz", hiddens.get("wjbz", "")),
        ]
        pairs.extend(answers)
        pairs.append(("zgpj", (zgpj_text or "")[:100]))  # 主观，≤100 字

        r = self._post_gbk("/jxpgXsAction.do?oper=wjpg", pairs)
        result = self._text_gbk(r)
        ok = ("评估成功" in result) or ("提交成功" in result) or ("成功" in result and "失败" not in result)
        return ok, result, len(questions)

    # ---------- 一键评估全部 ----------
    def evaluate_all(self, zgpj_provider, wait_seconds=5,
                     keep_one_second_best=True, stop_flag=None,
                     between_courses=3):
        """对所有未评课程依次：打开 -> 等待 -> 提交。

        zgpj_provider: 可调用，返回某门课的主观评价文本。
        wait_seconds:  打开问卷后到提交之间的等待秒数（应对服务端 3 分钟限制）。
        stop_flag:     可调用，返回 True 时中止。
        """
        courses = self.get_courses()
        if not courses:
            self.log("没有需要评估的课程（可能都已评完）。")
            return 0, 0
        self.log("共发现 %d 门未评估课程。" % len(courses))
        done = 0
        for idx, c in enumerate(courses, 1):
            if stop_flag and stop_flag():
                self.log("已手动停止。")
                break
            tag = "[%d/%d] %s - %s" % (idx, len(courses), c["pgnrm"], c["bprm"])
            self.log("%s  打开问卷..." % tag)
            try:
                html = self.open_questionnaire(c)
                _, questions = self.parse_form(html)
                self.log("%s  解析到 %d 道题，开始等待 %d 秒（规避提交时间限制）..."
                         % (tag, len(questions), wait_seconds))
                # 分段等待以便能及时响应停止
                waited = 0
                while waited < wait_seconds:
                    if stop_flag and stop_flag():
                        break
                    step = min(1, wait_seconds - waited)
                    time.sleep(step)
                    waited += step
                if stop_flag and stop_flag():
                    self.log("已手动停止。")
                    break
                zgpj = zgpj_provider(c) if callable(zgpj_provider) else zgpj_provider
                ok, result, nq = self.submit(html, zgpj, keep_one_second_best)
                if ok:
                    done += 1
                    self.log("%s  ✅ 提交成功" % tag)
                else:
                    snippet = re.sub(r"\s+", " ", result)[:120]
                    self.log("%s  ❌ 提交失败：%s" % (tag, snippet))
            except Exception as e:
                self.log("%s  ❌ 异常：%s" % (tag, e))
            time.sleep(between_courses)
        self.log("完成：成功 %d / 共 %d。" % (done, len(courses)))
        return done, len(courses)


# ============================================================
#  自测：用本地保存的 HTML 验证解析与答案构建（无需联网）
# ============================================================
def selftest():
    import os
    here = os.path.dirname(os.path.abspath(__file__))

    def load(path):
        with open(path, "rb") as f:
            return f.read().decode("gbk", errors="replace")

    print("=== 自测：解析问卷表单 ===")
    form_candidates = [
        os.path.join(here, "教学评估_files", "saved_resource.html"),
    ]
    form_html = None
    for p in form_candidates:
        if os.path.exists(p):
            form_html = load(p)
            break
    if form_html:
        hiddens, questions = URPClient.parse_form(form_html)
        print("隐藏字段:", hiddens)
        print("题目数量:", len(questions))
        answers = URPClient.build_answers(questions, keep_one_second_best=True)
        for (name, val), q in zip(answers, questions):
            ids = [o[0] for o in q["options"]]
            print("  题 %s 选项id=%s -> 提交值 %s" % (name, ids, val))
        # 校验防作弊：被选中的"id"不能全相同
        chosen_ids = []
        for (name, val), q in zip(answers, questions):
            for rid, v in q["options"]:
                if v == val:
                    chosen_ids.append(rid)
                    break
        print("被选中的选项 id 集合:", sorted(set(chosen_ids)),
              "=> 通过同答案校验" if len(set(chosen_ids)) > 1 else "=> ⚠会被拦截")
    else:
        print("未找到本地问卷表单文件，跳过。")

    print("\n=== 自测：解析课程列表 ===")
    list_candidates = [
        os.path.join(here, "URP综合教务系统-教学评估-评估-教学评估_files", "saved_resource.html"),
    ]
    list_html = None
    for p in list_candidates:
        if os.path.exists(p):
            list_html = load(p)
            break
    if list_html:
        c = URPClient(log=print)
        # 直接复用正则解析逻辑
        pattern = re.compile(
            r'<img\s+name="([^"]*#@[^"]*)"[^>]*onclick="evaluation\(this\)"', re.I)
        rows = pattern.findall(list_html)
        print("可评估课程数:", len(rows))
        for raw in rows[:5]:
            parts = raw.split("#@")
            print("  %s | 老师=%s | 课程=%s" % (parts[0], parts[2], parts[4]))
    else:
        print("未找到本地列表文件，跳过。")
    print("\n自测完成。")


# ============================================================
#  图形界面（tkinter）
# ============================================================
def run_gui():
    import tkinter as tk
    from tkinter import ttk, messagebox

    root = tk.Tk()
    root.title("紫金 URP 自动评估软件")
    root.geometry("760x640")

    log_q = queue.Queue()

    def gui_log(msg):
        log_q.put(str(msg))

    client = URPClient(log=gui_log)
    state = {"captcha_img": None, "logged_in": False, "stop": False,
             "running": False}

    # ---------- 顶部：连接与账号 ----------
    frm = ttk.Frame(root, padding=10)
    frm.pack(fill="x")

    ttk.Label(frm, text="服务器地址").grid(row=0, column=0, sticky="w")
    server_var = tk.StringVar(value=DEFAULT_SERVER)
    ttk.Entry(frm, textvariable=server_var, width=40).grid(row=0, column=1, columnspan=3, sticky="w", padx=4, pady=3)

    ttk.Label(frm, text="学号").grid(row=1, column=0, sticky="w")
    zjh_var = tk.StringVar()
    ttk.Entry(frm, textvariable=zjh_var, width=22).grid(row=1, column=1, sticky="w", padx=4, pady=3)

    ttk.Label(frm, text="密码").grid(row=1, column=2, sticky="w")
    mm_var = tk.StringVar()
    ttk.Entry(frm, textvariable=mm_var, width=18, show="*").grid(row=1, column=3, sticky="w", padx=4, pady=3)

    ttk.Label(frm, text="验证码").grid(row=2, column=0, sticky="w")
    yzm_var = tk.StringVar()
    ttk.Entry(frm, textvariable=yzm_var, width=10).grid(row=2, column=1, sticky="w", padx=4, pady=3)

    captcha_label = ttk.Label(frm, text="(点『刷新验证码』)", relief="solid")
    captcha_label.grid(row=2, column=2, sticky="w", padx=4)

    ocr_hint = "自动识别:开" if HAS_OCR else "自动识别:未安装(手动输入)"
    ttk.Label(frm, text=ocr_hint, foreground=("green" if HAS_OCR else "red")).grid(
        row=2, column=3, sticky="w")

    # ---------- 中部：策略设置 ----------
    opt = ttk.LabelFrame(root, text="评估设置", padding=10)
    opt.pack(fill="x", padx=10)

    ttk.Label(opt, text="每份问卷提交前等待(秒)").grid(row=0, column=0, sticky="w")
    wait_var = tk.IntVar(value=5)
    ttk.Entry(opt, textvariable=wait_var, width=8).grid(row=0, column=1, sticky="w", padx=4)
    ttk.Label(opt, text="(默认5秒。若提交报『未到提交时间』，说明服务端校验时间，请改回185秒/门)",
              foreground="gray").grid(row=0, column=2, columnspan=3, sticky="w")

    second_best_var = tk.BooleanVar(value=True)
    ttk.Checkbutton(opt, text="留最后1题选次高分(推荐，规避『答案不可一样』)",
                    variable=second_best_var).grid(row=1, column=0, columnspan=4, sticky="w", pady=3)

    ttk.Label(opt, text="主观评价(≤100字，留空则用内置模板逐课轮换)").grid(row=2, column=0, columnspan=4, sticky="w")
    zgpj_text = tk.Text(opt, height=2, width=88)
    zgpj_text.grid(row=3, column=0, columnspan=5, sticky="we", pady=3)

    # ---------- 按钮 ----------
    btns = ttk.Frame(root, padding=(10, 4))
    btns.pack(fill="x")
    btn_captcha = ttk.Button(btns, text="刷新验证码")
    btn_captcha.pack(side="left")
    btn_login = ttk.Button(btns, text="登录")
    btn_login.pack(side="left", padx=6)
    btn_run = ttk.Button(btns, text="开始自动评估", state="disabled")
    btn_run.pack(side="left", padx=6)
    btn_stop = ttk.Button(btns, text="停止", state="disabled")
    btn_stop.pack(side="left")

    # ---------- 日志 ----------
    logbox = tk.Text(root, height=18, wrap="word", state="disabled", bg="#111", fg="#ddd")
    logbox.pack(fill="both", expand=True, padx=10, pady=8)

    def drain_log():
        try:
            while True:
                msg = log_q.get_nowait()
                logbox.config(state="normal")
                logbox.insert("end", msg + "\n")
                logbox.see("end")
                logbox.config(state="disabled")
        except queue.Empty:
            pass
        root.after(150, drain_log)

    # ---------- 验证码 ----------
    def refresh_captcha():
        client.base = server_var.get().strip().rstrip("/")
        try:
            img = client.fetch_captcha()
        except Exception as e:
            gui_log("获取验证码失败: %s" % e)
            return
        # 自动识别
        if HAS_OCR:
            code = client.recognize_captcha(img)
            if code:
                yzm_var.set(code)
                gui_log("验证码自动识别: %s（如不对请手动改）" % code)
        # 显示图片
        if HAS_PIL:
            try:
                import io
                im = Image.open(io.BytesIO(img))
                im = im.resize((90, 30))
                ph = ImageTk.PhotoImage(im)
                state["captcha_img"] = ph
                captcha_label.config(image=ph, text="")
            except Exception as e:
                captcha_label.config(text="图显示失败")
                gui_log("验证码显示失败: %s" % e)
        else:
            try:
                with open("captcha.png", "wb") as f:
                    f.write(img)
                captcha_label.config(text="见 captcha.png")
                gui_log("未装 Pillow，验证码已存为 captcha.png，请打开查看后手动输入。")
            except Exception:
                pass

    def do_login():
        client.base = server_var.get().strip().rstrip("/")
        if not zjh_var.get().strip() or not mm_var.get():
            messagebox.showwarning("提示", "请填写学号和密码")
            return
        if not yzm_var.get().strip():
            messagebox.showwarning("提示", "请先刷新并填写验证码")
            return

        def worker():
            try:
                gui_log("正在登录...")
                client.login(zjh_var.get(), mm_var.get(), yzm_var.get())
                state["logged_in"] = True
                gui_log("✅ 登录成功！点『开始自动评估』。")
                btn_run.config(state="normal")
            except Exception as e:
                state["logged_in"] = False
                gui_log("❌ 登录失败: %s" % e)
                root.after(0, refresh_captcha)  # 换新验证码方便重试
        threading.Thread(target=worker, daemon=True).start()

    def zgpj_provider(course):
        custom = zgpj_text.get("1.0", "end").strip()
        if custom:
            return custom
        return random.choice(SUBJECTIVE_TEMPLATES)

    def do_run():
        if not state["logged_in"]:
            messagebox.showwarning("提示", "请先登录")
            return
        if state["running"]:
            return
        state["stop"] = False
        state["running"] = True
        btn_run.config(state="disabled")
        btn_stop.config(state="normal")

        def worker():
            try:
                client.evaluate_all(
                    zgpj_provider=zgpj_provider,
                    wait_seconds=max(0, int(wait_var.get())),
                    keep_one_second_best=second_best_var.get(),
                    stop_flag=lambda: state["stop"],
                )
            except Exception as e:
                gui_log("运行异常: %s" % e)
            finally:
                state["running"] = False
                root.after(0, lambda: (btn_run.config(state="normal"),
                                       btn_stop.config(state="disabled")))
        threading.Thread(target=worker, daemon=True).start()

    def do_stop():
        state["stop"] = True
        gui_log("正在停止（当前等待结束后中止）...")

    btn_captcha.config(command=refresh_captcha)
    btn_login.config(command=do_login)
    btn_run.config(command=do_run)
    btn_stop.config(command=do_stop)

    gui_log("欢迎使用紫金 URP 自动评估软件。")
    gui_log("步骤：① 填学号密码 ② 点『刷新验证码』(自动识别会预填) ③ 点『登录』 ④ 点『开始自动评估』。")
    if not HAS_OCR:
        gui_log("提示：未安装 ddddocr，验证码需手动输入（看右侧小图）。装上可自动识别：pip install ddddocr")
    drain_log()
    root.after(200, refresh_captcha)  # 启动即拉一张验证码
    root.mainloop()


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] in ("--selftest", "-t", "selftest"):
        selftest()
    else:
        run_gui()
