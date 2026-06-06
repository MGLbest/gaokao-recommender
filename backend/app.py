"""高考志愿填报 API v3 — 基于 v3 数据库, location 字段精确判断省内/省外"""
import sys, os
from collections import defaultdict
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from flask import Flask, request, jsonify
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

import sqlite3
DB = 'data/henan_gaokao_v3.db'

def get_db():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    return conn

def calculate_prob(student_rank, school_ranks):
    """简化版概率计算"""
    import math
    if not school_ranks or student_rank is None:
        return 0.0
    ranks = [r for r in school_ranks if r and r > 999]
    if not ranks:
        return 0.0
    avg = sum(ranks) / len(ranks)
    n = len(ranks)
    std = max(math.sqrt(sum((r-avg)**2 for r in ranks) / n), avg * 0.05) if n >= 2 else avg * 0.15
    z = (avg - student_rank) / std if std > 0 else 0
    prob = 0.5 * (1 + math.erf(z / math.sqrt(2)))
    if n == 1: prob = max(0.10, min(0.85, prob))
    return max(0.05, min(0.95, prob))

def score_to_rank(score, category, province):
    """分数→位次"""
    db = get_db()
    cur = db.cursor()
    cat_map = {'physics':(1,4), 'history':(2,5), 'comprehensive':(3,), 'science':(4,1), 'arts':(5,2)}
    cats = cat_map.get(category, (1,))
    ph = ','.join('?' * len(cats))
    cur.execute(f"SELECT cumulative_count FROM score_distribution WHERE province_id=(SELECT id FROM provinces WHERE name=?) AND category_id IN ({ph}) AND year=2025 AND score=? LIMIT 1",
        (province, *cats, score))
    row = cur.fetchone()
    if row and row[0]:
        db.close(); return row[0]
    # Interpolate
    cur.execute(f"SELECT score, cumulative_count FROM score_distribution WHERE province_id=(SELECT id FROM provinces WHERE name=?) AND category_id IN ({ph}) AND year=2025 ORDER BY score DESC",
        (province, *cats))
    rows = cur.fetchall()
    db.close()
    if not rows: return None
    above = below = None
    for r in rows:
        if r[0] >= score: above = r
        else: below = r; break
    if above and below:
        sr = above[0] - below[0]
        rr = below[1] - above[1]
        if sr > 0: return int(above[1] + (above[0] - score) / sr * rr)
        return above[1]
    return above[1] if above else (below[1] if below else None)


@app.route("/api/predict", methods=["POST"])
def api_predict():
    data = request.get_json()
    province = data.get("province", "河南")
    category = data.get("category", "physics")
    score = int(data.get("score", 0) or 0)
    rank = data.get("rank") or None
    strategy = data.get("strategy", "balanced")

    if not province:
        return jsonify({"error": "缺少省份"}), 400
    if not rank and (score < 100 or score > 750):
        return jsonify({"error": "请输入有效位次或分数"}), 400

    student_rank = rank if rank else score_to_rank(score, category, province)
    if not student_rank:
        return jsonify({"error": f"找不到{province}位次"}), 400

    db = get_db()
    cur = db.cursor()
    cats = {'physics':(1,4), 'history':(2,5), 'comprehensive':(3,), 'science':(4,1), 'arts':(5,2)}.get(category, (1,))
    ph = ','.join('?' * len(cats))

    # 查该省所有学校投档数据
    cur.execute(f"""SELECT s.id, s.name, ss.year, ss.min_score, ss.min_rank
        FROM school_scores ss JOIN schools s ON ss.school_id=s.id
        WHERE ss.province_id=(SELECT id FROM provinces WHERE name=?)
        AND ss.category_id IN ({ph}) AND ss.min_rank IS NOT NULL AND ss.min_score>=400
        AND s.name NOT LIKE '%职业%' AND s.name NOT LIKE '%专科%'
        ORDER BY s.name, ss.year DESC""", (province, *cats))

    school_data = defaultdict(list)
    for r in cur.fetchall():
        school_data[(r[0], r[1])].append((r[2], r[3], r[4]))

    # 查所有学校 location
    cur.execute("SELECT id, name, location, nature, is_985, is_211, level, city_tier_id, national_rank FROM schools")
    school_info = {}
    for r in cur.fetchall():
        school_info[r[0]] = dict(r)

    # 计算概率
    results = []
    for (sid, sname), hist in school_data.items():
        lt = hist[0]
        rks = [(y, rk) for y, _, rk in hist if rk and rk > 999]
        if not rks: continue
        prob = calculate_prob(student_rank, rks)
        results.append({
            "school_name": sname, "latest_score": lt[1], "latest_rank": lt[2],
            "prob": round(prob, 3), "school_id": sid
        })

    results.sort(key=lambda x: x["prob"], reverse=True)

    # 省内/省外分离 (用 schools.location)
    in_p = {"冲": [], "稳": [], "保": []}
    out_p = {"冲": [], "稳": [], "保": []}

    for r in results:
        si = school_info.get(r["school_id"], {})
        sloc = si.get("location", "")
        is_in = province in sloc if sloc else False

        pr = r["prob"]
        lv = "保" if pr >= 0.65 else ("稳" if pr >= 0.35 else "冲")
        target = in_p if is_in else out_p

        r["location"] = sloc
        r["nature"] = si.get("nature", "")
        r["is_985"] = bool(si.get("is_985", 0))
        r["is_211"] = bool(si.get("is_211", 0))
        r["level"] = si.get("level", "普通")
        r["city_tier"] = si.get("city_tier_id", 3)
        r["national_rank"] = si.get("national_rank")

        if len(target[lv]) < 50:
            target[lv].append(r)

    db.close()
    return jsonify({
        "province": province, "category": category, "score": score, "rank": student_rank,
        "strategy": strategy,
        "in_province": {**in_p, "total": sum(len(v) for v in in_p.values())},
        "out_province": {**out_p, "total": sum(len(v) for v in out_p.values())},
    })


@app.route("/api/school-majors", methods=["POST"])
def api_school_majors():
    data = request.get_json()
    province = data.get("province", "河南")
    category = data.get("category", "physics")
    school_name = data.get("school_name", "")
    score = int(data.get("score", 0) or 0)
    rank = data.get("rank") or None

    if not school_name:
        return jsonify({"error": "缺少学校名"}), 400

    student_rank = rank if rank else score_to_rank(score, category, province)
    db = get_db()
    cur = db.cursor()
    cats = {'physics':(1,4), 'history':(2,5), 'comprehensive':(3,)}.get(category, (1,))
    ph = ','.join('?' * len(cats))

    # 专业历史
    cur.execute(f"""SELECT m.major_name, m.year, m.min_score, m.min_rank, m.subject_requirement, m.duration, m.degree_type, m.major_tag
        FROM major_scores m JOIN schools s ON m.school_id=s.id
        WHERE s.name=? AND m.province_id=(SELECT id FROM provinces WHERE name=?) AND m.category_id IN ({ph})
        ORDER BY m.major_name, m.year DESC LIMIT 500""", (school_name, province, *cats))

    mh = defaultdict(list)
    for r in cur.fetchall():
        mh[r[0]].append(dict(year=r[1], score=r[2], rank=r[3], subj=r[4], duration=r[5], degree=r[6], tag=r[7]))

    # 招生计划
    cur.execute("""SELECT major_name, plan_count, tuition_fee, year FROM enrollment_plans
        WHERE school_id=(SELECT id FROM schools WHERE name=?) AND province_id=(SELECT id FROM provinces WHERE name=?)
        ORDER BY year DESC""", (school_name, province))
    plans = defaultdict(dict)
    for r in cur.fetchall():
        plans[r[0]][r[3]] = (r[1], r[2])

    # 优势专业
    cur.execute(f"""SELECT m.major_name, AVG(m.min_rank), COUNT(*) FROM major_scores m
        WHERE m.school_id=(SELECT id FROM schools WHERE name=?) AND m.province_id=(SELECT id FROM provinces WHERE name=?) AND m.category_id IN ({ph})
        GROUP BY m.major_name HAVING COUNT(*)>=2 ORDER BY AVG(m.min_rank) ASC LIMIT 10""",
        (school_name, province, *cats))
    strengths = [{"major_name": r[0], "avg_rank": int(r[1] or 0)} for r in cur.fetchall()]

    # 可报专业
    available = []
    for mn, hist in mh.items():
        plan_data = plans.get(mn, {}).get(2025, (0, None))
        pc = plan_data[0]
        tf = plan_data[1]
        rks = [(d["year"], d["rank"]) for d in hist if d.get("rank")]
        prob = calculate_prob(student_rank, rks) if rks else (0.5 if pc > 0 else 0.3)
        lt = hist[0] if hist else {}
        available.append(dict(
            major_name=mn, latest_score=lt.get("score"), latest_rank=lt.get("rank"),
            plan_count=pc, tuition_fee=tf, prob=round(prob, 3),
            subject_requirement=lt.get("subj"), duration=lt.get("duration"),
            degree_type=lt.get("degree"), major_tag=lt.get("tag"),
            history=[dict(year=d["year"], score=d["score"], rank=d["rank"]) for d in hist[:3]],
        ))
    available.sort(key=lambda x: x["prob"], reverse=True)

    db.close()
    return jsonify(dict(
        school_name=school_name, student_rank=student_rank,
        available_majors=available[:30], strengths=strengths,
        total_plans=sum(p[0] for p in plans.values() for p in p.values()),
    ))


@app.route("/api/provinces", methods=["GET"])
def api_provinces():
    db = get_db()
    cur = db.cursor()
    cur.execute("""SELECT p.name, p.exam_pop_2025,
        (SELECT COUNT(*) FROM school_scores WHERE province_id=p.id) as sc,
        (SELECT COUNT(*) FROM score_distribution WHERE province_id=p.id) as sd
        FROM provinces p ORDER BY p.id""")
    result = [dict(r) for r in cur.fetchall()]
    db.close()
    return jsonify(result)


@app.route("/api/school/<name>", methods=["GET"])
def api_school(name):
    province = request.args.get("province", "")
    db = get_db()
    cur = db.cursor()
    if province:
        cur.execute("""SELECT ss.year, ss.min_score, ss.min_rank, ss.category
            FROM school_scores ss JOIN schools s ON ss.school_id=s.id JOIN provinces p ON ss.province_id=p.id
            WHERE s.name=? AND p.name=? AND ss.year>=2023 ORDER BY ss.year DESC""", (name, province))
    else:
        cur.execute("""SELECT ss.year, ss.min_score, ss.min_rank, ss.category, p.name as prov
            FROM school_scores ss JOIN schools s ON ss.school_id=s.id JOIN provinces p ON ss.province_id=p.id
            WHERE s.name=? AND ss.year>=2023 ORDER BY p.name, ss.year DESC""", (name,))

    cur.execute("SELECT location, nature, is_985, is_211, level, city_tier_id, national_rank FROM schools WHERE name=?", (name,))
    sinfo = cur.fetchone()
    db.close()

    return jsonify({
        "school_name": name,
        "info": dict(sinfo) if sinfo else {},
        "history": [dict(year=r[0], score=r[1], rank=r[2], category=r[3]) for r in cur.fetchall()],
    })


@app.route("/", methods=["GET"])
def home():
    return jsonify({"service": "高考志愿填报预测API v3", "version": "3.0", "database": DB})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
