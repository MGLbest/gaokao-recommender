"""
Gaokao Volunteer Recommendation System v3 - Optimized
Fast in-memory matching with simple DB queries.
"""
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
import sqlite3, os, math, json

app = Flask(__name__, static_folder='static', static_url_path='')
CORS(app)

DB = os.environ.get("DB_PATH", os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "gaokao_v4.db"))

def ensure_db():
    """Recombine DB from chunks if DB file doesn't exist (for deployment)."""
    if os.path.exists(DB):
        return
    chunk_dir = os.path.join(os.path.dirname(DB), "db_chunks")
    if not os.path.exists(chunk_dir):
        return
    print(f"Recombining database from chunks...")
    chunks = sorted([c for c in os.listdir(chunk_dir) if c.startswith("chunk_")])
    with open(DB, 'wb') as out:
        for c in chunks:
            with open(os.path.join(chunk_dir, c), 'rb') as cf:
                out.write(cf.read())
    print(f"Database recombined: {os.path.getsize(DB)/1024/1024:.0f}MB")
    # Remove chunks after recombination to save disk
    import shutil
    shutil.rmtree(chunk_dir, ignore_errors=True)
    print("Chunks cleaned up")

ensure_db()

# ── Global cache (loaded once) ──
SCHOOLS = {}       # id -> row
MAJORS = {}        # id -> row
SCHOOL_MAJORS = {} # (school_id, major_id) -> row
PROVINCES = []

def load_cache():
    global SCHOOLS, MAJORS, SCHOOL_MAJORS, PROVINCES
    db = sqlite3.connect(DB)
    db.row_factory = sqlite3.Row
    for r in db.execute("SELECT * FROM schools"):
        d = {k: r[k] for k in r.keys()}
        SCHOOLS[d['id']] = d
    for r in db.execute("SELECT * FROM majors"):
        d = {k: r[k] for k in r.keys()}
        MAJORS[d['id']] = d
    for r in db.execute("SELECT * FROM school_majors"):
        d = {k: r[k] for k in r.keys()}
        SCHOOL_MAJORS[(d['school_id'], d['major_id'])] = d
    PROVINCES = [{'id': r['id'], 'name': r['name']} for r in db.execute("SELECT id, name FROM provinces ORDER BY folder_order").fetchall()]
    db.close()
    print(f"Cache loaded: {len(SCHOOLS)} schools, {len(MAJORS)} majors, {len(SCHOOL_MAJORS)} sm, {len(PROVINCES)} provinces")

load_cache()

def get_db():
    db = sqlite3.connect(DB)
    db.row_factory = sqlite3.Row
    return db

# ── Routes ──

@app.route('/')
def index():
    return send_from_directory('static', 'index.html')

@app.route('/api/provinces')
def list_provinces():
    return jsonify(PROVINCES)

@app.route('/api/recommend', methods=['POST'])
def recommend():
    data = request.json or {}
    province_id = data.get('province_id')
    category = data.get('category', 'physics')
    user_rank = data.get('rank')
    user_score = data.get('score')
    subjects = set(data.get('subjects', []))
    language = data.get('language', '')
    color_blind = data.get('color_blind', False)
    gender = data.get('gender', '')
    preferred_cities = set(data.get('cities', []))
    preferred_majors = set(data.get('major_categories', []))
    max_tuition = data.get('max_tuition')
    priority = data.get('priority', 'balanced')

    if not province_id:
        return jsonify({'success': False, 'error': '请选择省份'})
    province_id = int(province_id)

    db = get_db()

    # ── Step 1: Score distribution lookup ──
    eq_score = user_score; same_count = 0; line_diff = None; batch_line_score = None; batch_line_name = None

    # If no rank but have score, estimate rank from score_distribution
    if not user_rank and user_score:
        row = db.execute("""
            SELECT cumulative_count, section_count FROM score_distribution
            WHERE province_id=? AND year=2025 AND category=? AND score=?
        """, (province_id, category, user_score)).fetchone()
        if row:
            user_rank = row['cumulative_count']
            same_count = row['section_count'] or 0

    if user_rank:
        row = db.execute("""
            SELECT score, section_count FROM score_distribution
            WHERE province_id=? AND year=2025 AND category=? AND cumulative_count >= ?
            ORDER BY cumulative_count ASC LIMIT 1
        """, (province_id, category, user_rank)).fetchone()
        if not row:
            row = db.execute("""
                SELECT score, section_count FROM score_distribution
                WHERE province_id=? AND year=2025 AND category=?
                ORDER BY ABS(cumulative_count - ?) LIMIT 1
            """, (province_id, category, user_rank)).fetchone()
        if row:
            eq_score = row['score']
            same_count = row['section_count'] or 0

    # If STILL no rank (no score_distribution data for this province), default all to 'wen'
    if not user_rank:
        user_rank = 0

    # Batch lines
    bl_rows = db.execute("""
        SELECT batch, score FROM score_lines
        WHERE province_id=? AND year=2025 AND category=?
        ORDER BY score DESC
    """, (province_id, category)).fetchall()
    if bl_rows:
        ug = next((b for b in bl_rows if '本科' in str(b['batch']) and '提前' not in str(b['batch']) and '特殊' not in str(b['batch']) and '艺' not in str(b['batch'])), None) or bl_rows[0]
        batch_line_score = ug['score']
        batch_line_name = ug['batch']
        if user_score: line_diff = user_score - batch_line_score

    # ── Step 2: Fast query - get all matching admission records ──
    conds = ["a.province_id=? AND a.category=? AND a.min_rank IS NOT NULL AND a.min_score IS NOT NULL AND a.year >= 2022"]
    params = [province_id, category]

    # Subject filter: build LIKE clauses
    if subjects:
        subj_parts = []
        for s in subjects:
            subj_parts.append("a.subject_requirement LIKE ?")
            params.append(f"%{s}%")
        conds.append(f"({' OR '.join(subj_parts)})")

    # Exclude vocational
    conds.append("a.batch NOT LIKE '%专科%' AND a.batch NOT LIKE '%高职%'")

    sql = f"""
        SELECT a.school_id, a.major_id, a.school_major_id, a.year, a.batch,
               a.min_score, a.min_rank, a.subject_requirement, a.enrollment_count
        FROM admission_history a
        WHERE {' AND '.join(conds)}
    """
    rows = db.execute(sql, params).fetchall()
    db.close()

    # ── Step 3: Aggregate by (school, major) ──
    stats = {}
    for r in rows:
        key = (r['school_id'], r['major_id'])
        if key not in stats:
            stats[key] = {'years': [], 'ranks': [], 'scores': [], 'batches': [], 'subjects': [],
                          'sm_id': r['school_major_id']}
        stats[key]['years'].append(r['year'])
        stats[key]['ranks'].append(r['min_rank'])
        stats[key]['scores'].append(r['min_score'])
        stats[key]['batches'].append(r['batch'])
        stats[key]['subjects'].append(r['subject_requirement'])

    # ── Step 4: Filter + Score ──
    results = []
    for (sid, mid), s in stats.items():
        school = SCHOOLS.get(sid)
        major = MAJORS.get(mid)
        sm = SCHOOL_MAJORS.get((sid, mid))
        if not school or not major: continue

        # Hard filter: physical exam
        if color_blind and sm and sm.get('physical_exam_req'):
            preq = str(sm['physical_exam_req'])
            if '色盲' in preq or '色弱' in preq: continue

        # Hard filter: language
        if language and sm and sm.get('language_req'):
            lreq = str(sm['language_req'])
            if language not in lreq: continue

        # Hard filter: gender
        if gender and sm and sm.get('gender_restrict'):
            greq = sm['gender_restrict']
            if gender == 'male' and greq == 'female_only': continue
            if gender == 'female' and greq == 'male_only': continue

        # Tuition filter
        if max_tuition and sm and sm.get('tuition') and sm['tuition'] > max_tuition: continue

        # Single subject filter (warn but don't exclude unless we know the user's subject scores)
        single_req = sm.get('single_subject_req') if sm else None

        # Compute reference rank
        ref_rank = min(s['ranks'])
        avg_rank = sum(s['ranks']) / len(s['ranks'])
        avg_score = sum(s['scores']) / len(s['scores'])

        # 冲稳保 - require rank for proper classification
        if user_rank and user_rank > 0:
            pct = (ref_rank - user_rank) / user_rank * 100
            if pct < -10: risk_level, risk_label, risk_desc = 'chong', '冲刺', '可能有难度'
            elif -10 <= pct <= 5: risk_level, risk_label, risk_desc = 'wen', '稳妥', '录取希望较大'
            else: risk_level, risk_label, risk_desc = 'bao', '保底', '录取把握很大'
        else:
            risk_level, risk_label, risk_desc = 'wen', '稳妥', '请提供位次以获得更准分类'

        # Multi-dimensional score
        total = 0
        if school.get('is_985'): total += 30
        if school.get('is_211'): total += 20
        if school.get('level') == '本科': total += 10
        if school.get('city') in preferred_cities: total += 5
        if major.get('category') in preferred_majors: total += 5
        if priority == 'school_first': total = total * 1.5 if school.get('is_985') or school.get('is_211') else total
        elif priority == 'major_first' and major.get('category') in preferred_majors: total += 10

        results.append({
            'school_id': sid, 'major_id': mid,
            'school_name': school['name'], 'major_name': major['name'],
            'major_category': major['category'] or '',
            'city': school.get('city', ''),
            'school_level': school.get('level', ''),
            'is_985': school.get('is_985', 0), 'is_211': school.get('is_211', 0),
            'ref_rank': ref_rank, 'avg_rank': round(avg_rank), 'avg_score': round(avg_score),
            'risk_level': risk_level, 'risk_label': risk_label, 'risk_desc': risk_desc,
            'years_data': len(s['years']), 'all_ranks': sorted(s['ranks']),
            'single_subject_req': single_req,
            'gender_restrict': sm.get('gender_restrict') if sm else None,
            'physical_exam_req': sm.get('physical_exam_req') if sm else None,
            'language_req': sm.get('language_req') if sm else None,
            'tuition': sm.get('tuition') if sm else None,
            'duration': sm.get('duration') if sm else None,
            'total_score': round(total, 1),
            'batch': s['batches'][0] if s['batches'] else '',
        })

    # Sort
    ro = {'chong': 0, 'wen': 1, 'bao': 2, 'unknown': 3}
    results.sort(key=lambda x: (ro.get(x['risk_level'], 4), -x['total_score']))

    chong_list = [r for r in results if r['risk_level'] == 'chong'][:30]
    wen_list = [r for r in results if r['risk_level'] == 'wen'][:30]
    bao_list = [r for r in results if r['risk_level'] == 'bao'][:30]

    total_count = len(chong_list) + len(wen_list) + len(bao_list)
    chong_pct = len(chong_list) / max(total_count, 1) * 100
    warnings = []
    if chong_pct > 50: warnings.append(f"冲刺志愿占比{chong_pct:.0f}%，滑档风险较高，建议增加稳妥志愿")
    if len(wen_list) < 3: warnings.append(f"稳妥志愿仅{len(wen_list)}个，建议补充至5-10个")
    if len(bao_list) < 2: warnings.append(f"保底志愿仅{len(bao_list)}个，存在滑档风险")

    return jsonify({
        'success': True,
        'user': {
            'eq_score': eq_score, 'same_rank_count': same_count,
            'line_diff': line_diff, 'batch_line_score': batch_line_score, 'batch_line_name': batch_line_name,
        },
        'recommendations': {'chong': chong_list, 'wen': wen_list, 'bao': bao_list},
        'warnings': warnings, 'total_matched': len(results),
    })

@app.route('/api/school-detail', methods=['POST'])
def school_detail():
    data = request.json or {}
    sid, mid, pid = data.get('school_id'), data.get('major_id'), data.get('province_id')

    school = SCHOOLS.get(sid) if sid else None
    major = MAJORS.get(mid) if mid else None
    sm = SCHOOL_MAJORS.get((sid, mid)) if sid and mid else None

    db = get_db()
    history = []
    if sid and mid and pid:
        history = [dict(r) for r in db.execute("""
            SELECT year, batch, min_score, min_rank, enrollment_count, subject_requirement
            FROM admission_history WHERE school_id=? AND major_id=? AND province_id=? ORDER BY year DESC
        """, (sid, mid, pid)).fetchall()]
    db.close()

    return jsonify({
        'success': True,
        'detail': {
            'school': school, 'major': major, 'school_major': sm,
            'history': history,
        }
    })

if __name__ == '__main__':
    print(f"Gaokao System v3 starting... DB: {os.path.getsize(DB)/1024/1024:.0f}MB")
    print("\n  For public access, run in another terminal:")
    print('  ssh -R 80:localhost:5000 serveo.net')
    print()

    app.run(host='0.0.0.0', port=5000, debug=False, threaded=True)
