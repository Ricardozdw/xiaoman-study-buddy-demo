"""检查上线配置与产物，可显式保存报告；返回非零即仍有待办，不创建部署。"""
import argparse
from datetime import datetime, timezone
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import sys
from urllib.parse import unquote, urlsplit

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


class EntryAssets(HTMLParser):
    def __init__(self):
        super().__init__()
        self.urls = []

    def handle_starttag(self, tag, attributes):
        attrs = dict(attributes)
        if tag == "script" and attrs.get("src"):
            self.urls.append(attrs["src"])
        if tag == "link" and "stylesheet" in (attrs.get("rel") or "").split() and attrs.get("href"):
            self.urls.append(attrs["href"])


def frontend_ready(dist):
    """检查将要打包的真实文件，不能只靠首页中出现过资源路径。"""
    try:
        root = Path(dist).resolve()
        parser = EntryAssets()
        parser.feed((root / "index.html").read_text(encoding="utf-8"))
        extensions = set()
        for raw in parser.urls:
            url = urlsplit(raw)
            path = unquote(url.path)
            if url.scheme or url.netloc or not path.startswith("/study/assets/"):
                return False, "JS/CSS必须来自本站 /study/assets/；请用 --base=/study/ 构建"
            resource = (root / path.removeprefix("/study/")).resolve()
            if not resource.is_relative_to(root) or resource.suffix not in (".js", ".css"):
                return False, "首页引用的资源路径不符合部署约定"
            if not resource.is_file() or resource.stat().st_size == 0:
                return False, "首页引用的JS/CSS缺失或为空，请重新完整构建"
            with resource.open("rb") as stream:
                prefix = stream.read(512).decode("utf-8-sig", errors="replace").lstrip().lower()
            if prefix.startswith(("<!doctype html", "<html")):
                return False, "JS/CSS实际为HTML错误页，请重新完整构建"
            extensions.add(resource.suffix)
        if extensions != {".js", ".css"}:
            return False, "首页缺少JS或CSS入口，请用 --base=/study/ 构建"
        avatar = (root / "xiaoman-lake-blue.png").resolve()
        if not avatar.is_relative_to(root) or not avatar.is_file():
            return False, "缺少小满原图，请确认web/public已随构建复制"
        with avatar.open("rb") as stream:
            if stream.read(8) != b"\x89PNG\r\n\x1a\n":
                return False, "小满图片不是PNG，请重新复制已确认原图"
    except (OSError, ValueError, UnicodeError):
        return False, "无法读取完整前端产物，请重新构建"
    return True, "已检查 /study/ 首页、JS、CSS及小满PNG；运行时仍须HTTP smoke"


def valid_origins(raw):
    if not raw or "*" in raw:
        return False
    try:
        for item in raw.split(","):
            url = urlsplit(item.strip())
            if url.scheme != "https" or not url.hostname or url.path or url.query or url.fragment or url.username or url.password:
                return False
            _ = url.port
    except ValueError:
        return False
    return True


def valid_databases(app_url, system_url):
    try:
        urls = [urlsplit(value) for value in (app_url, system_url)]
        for url in urls:
            if (url.scheme not in ("postgres", "postgresql", "postgresql+psycopg") or not url.hostname
                    or not url.username or not url.path.strip("/") or url.fragment):
                return False
            if url.username.split(".")[0].lower() in ("postgres", "supabase_admin", "service_role"):
                return False
            _ = url.port
        return urls[0].username != urls[1].username
    except (TypeError, ValueError, AttributeError):
        return False


def check(evidence=None):
    items=[]
    def add(name, passed, detail):
        items.append({"check":name,"passed":bool(passed),"detail":detail})
    add("会话签名", len(os.getenv("AUTH_SECRET", "").strip()) >= 32, "至少32字符，勿使用示例值")
    add("MVP范围", os.getenv("XIAOMAN_MVP") == "1", "XIAOMAN_MVP=1")
    add("关闭演示时钟", os.getenv("XIAOMAN_DEMO_CLOCK", "0").strip().lower() not in ("1", "true", "yes", "on"), "上线环境不能保留演示跳日")
    origins=os.getenv("CORS_ALLOW_ORIGINS", "")
    add("站点来源", valid_origins(origins), "填写实际HTTPS来源，包含主机且不带路径、账号或查询参数")
    frontend_ok, frontend_detail = frontend_ready(ROOT / "web/dist")
    add("前端产物", frontend_ok, frontend_detail)
    try:
        registry=json.loads(os.getenv("LLM_CONFIG_JSON", "{}"))
        if not isinstance(registry, dict):
            raise ValueError("模型配置必须是对象")
        models=registry.get("models", {})
        model_ready=True
        for tier in ("fast", "strong"):
            chain=registry.get("tiers", {}).get(tier, [])
            if isinstance(chain, str):
                chain=[model.strip() for model in chain.split(",")]
            if not isinstance(chain, list):
                raise ValueError("模型链必须是列表")
            specs=[models[name] for name in chain]
            has_models=len({spec["model"] for spec in specs if isinstance(spec["model"], str) and spec["model"].strip()}) >= 2
            has_keys=True
            for spec in specs:
                names=spec.get("api_key_env", [])
                names=[names] if isinstance(names, str) else names
                has_keys=has_keys and any(os.getenv(name, "").strip() for name in names)
            model_ready=model_ready and has_models and has_keys
    except (ValueError, TypeError, KeyError, AttributeError):
        model_ready=False
    add("主备模型", model_ready, "FAST和STRONG链都须含两个不同且已配置凭据的模型")
    add("数据库", valid_databases(os.getenv("DATABASE_URL", ""), os.getenv("SYSTEM_DATABASE_URL", "")), "分别使用不同的PostgreSQL app/system登录角色；实际权限仍需RLS验收")
    required=("unit_tests", "golden_rules", "golden_hybrid", "harness", "web_tests", "web_build", "study_rls", "public_pdf", "demo_8_steps", "manual_model_review", "fixed_cases_exported", "budget_approved", "persistent_storage", "live_evaluation", "dependency_review", "container_smoke")
    evidence=evidence if isinstance(evidence, dict) else {}
    for name in required:
        add(name, evidence.get(name) is True, "请填写有证据支持的验收结果")
    return {"ready":all(i["passed"] for i in items), "checks":items,
            "blockers":[i["check"] for i in items if not i["passed"]],
            "generated_at":datetime.now(timezone.utc).isoformat()}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("evidence", type=Path, nargs="?", help="已实际验收的JSON记录；未完成项保留false")
    parser.add_argument("--output", type=Path, help="显式保存JSON报告，不包含密钥或连接信息")
    args = parser.parse_args(argv)
    if args.evidence and args.output and args.evidence.resolve() == args.output.resolve():
        parser.error("报告不能覆盖验收记录，请使用不同的输出文件")
    evidence = None
    invalid_evidence = False
    if args.evidence:
        try:
            evidence = json.loads(args.evidence.read_text(encoding="utf-8"))
            if not isinstance(evidence, dict):
                raise ValueError("验收记录必须是对象")
        except (OSError, ValueError, UnicodeError):
            invalid_evidence = True
    report=check(evidence)
    if invalid_evidence:
        report["checks"].insert(0, {"check":"验收记录", "passed":False, "detail":"无法读取有效JSON对象；未采用该文件中的任何验收结论"})
        report["blockers"].insert(0, "验收记录")
        report["ready"] = False
    if args.output:
        try:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        except OSError:
            report["checks"].append({"check":"报告保存", "passed":False, "detail":"无法保存报告，请检查输出目录"})
            report["blockers"].append("报告保存")
            report["ready"] = False
    print(json.dumps(report,ensure_ascii=False,indent=2))
    return int(not report["ready"])


if __name__ == "__main__":
    raise SystemExit(main())
