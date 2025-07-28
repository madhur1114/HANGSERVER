import time
import threading
from collections import Counter, defaultdict
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
import requests

app = Flask(__name__, static_folder='.')
CORS(app)

# In-memory store: {period: {'prediction':..., 'actual_result':..., 'timestamp':..., 'real_result_checked':...}}
period_results = {}

# ========== CONFIGURATION ==========
API_URL = 'https://api.bdg88zf.com/api/webapi/GetNoaverageEmerdList'
API_TIMEOUT = 10  # seconds

# If you have an API for fetching actual results, set here:
ACTUAL_RESULT_API_URL = 'https://api.bdg88zf.com/api/webapi/GetNoaverageEmerdList'
# You may need to adapt logic to extract the actual result

# ========== PREDICTION LOGIC ==========

def extract_numbers_from_api(api_data):
    """
    Extract all numbers from the API response in a robust way.
    """
    numbers = []
    def recurse(data):
        if isinstance(data, dict):
            for v in data.values():
                recurse(v)
        elif isinstance(data, list):
            for x in data:
                recurse(x)
        elif isinstance(data, str):
            numbers.extend([int(a) for a in data.split() if a.isdigit()])
        elif isinstance(data, int):
            numbers.append(data)
    recurse(api_data)
    # If nothing found, fall back to random
    if not numbers:
        import random
        numbers = [random.randint(0, 9) for _ in range(20)]
    return numbers

def best_predictor(api_data):
    """
    Use the best possible logic for:
    - Big/Small
    - Two high frequency numbers (0-9)
    - Red/Green
    """
    nums = extract_numbers_from_api(api_data)
    cnt = Counter(nums)
    # Use most frequent numbers, in case of tie, pick the largest numbers
    freq_sorted = sorted(cnt.items(), key=lambda x: (-x[1], -x[0]))
    high_freq = [item for item, _ in freq_sorted[:2]]
    # If not enough, pad
    while len(high_freq) < 2:
        high_freq.append((high_freq[-1]+1) % 10 if high_freq else 0)
    # Big/Small: mean >= 5 is big, else small
    mean = sum(nums) / len(nums)
    big_small = "Big" if mean >= 5 else "Small"
    # Red/Green: sum even = green, odd = red
    red_green = "Green" if sum(nums) % 2 == 0 else "Red"
    return {
        "high_freq_numbers": high_freq,
        "big_small": big_small,
        "red_green": red_green
    }

def outcome_analysis(pred, actual):
    """
    Compare predicted numbers with actual result.
    - If predicted high freq contains actual: Jackpot.
    - If actual matches Big/Small: Win (Small)
    - Else: Loss
    """
    if not pred or actual is None:
        return {"outcome": "", "indicator": ""}
    high = [int(x) for x in pred["high_freq_numbers"]]
    actual = int(actual)
    if actual in high:
        return {"outcome": "Jackpot", "indicator": "jackpot"}
    pred_big_small = pred["big_small"]
    if (actual >= 5 and pred_big_small == "Big") or (actual < 5 and pred_big_small == "Small"):
        return {"outcome": "Win (Small)", "indicator": "win"}
    return {"outcome": "Loss", "indicator": "loss"}

# ========== API ROUTES ==========

@app.route('/api/predict', methods=['POST'])
def predict():
    data = request.json
    period = data.get('period')
    if not period or not (len(period) == 17 and period.isdigit()):
        return jsonify({"success": False, "error": "Invalid period"}), 400
    try:
        # Use actual API
        resp = requests.post(API_URL, json={"period": period}, timeout=API_TIMEOUT)
        api_data = resp.json()
        prediction = best_predictor(api_data)
        period_results[period] = {
            "prediction": prediction,
            "actual_result": None,
            "timestamp": time.time(),
            "real_result_checked": False
        }
        return jsonify({"success": True, "prediction": {**prediction, "period": period}})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@app.route('/api/results/<period>', methods=['GET', 'POST'])
def results(period):
    entry = period_results.get(period)
    if not entry:
        return jsonify({"error": "No prediction for this period"}), 404

    if request.method == 'POST':
        # User updates actual result
        data = request.json
        actual = data.get('actual')
        if actual is None or not str(actual).isdigit() or not (0 <= int(actual) <= 9):
            return jsonify({"error": "Actual result must be 0-9"}), 400
        entry['actual_result'] = int(actual)
        entry['timestamp'] = time.time()
        period_results[period] = entry

    pred = entry['prediction']
    actual = entry.get('actual_result')
    analysis = outcome_analysis(pred, actual) if actual is not None else {"outcome": "", "indicator": ""}
    return jsonify({
        "period": period,
        "prediction": '/'.join([str(x) for x in pred.get('high_freq_numbers', [])]),
        "actual_result": actual,
        "outcome": analysis["outcome"],
        "indicator": analysis["indicator"]
    })

@app.route('/', defaults={'path': 'index.html'})
@app.route('/<path:path>')
def serve_static(path):
    return send_from_directory('.', path)

# ========== BACKGROUND: AUTO-FETCH RESULTS ==========

def fetch_actual_result_for_period(period):
    """
    Fetch the actual result for a period from the API.
    This implementation assumes the API returns a JSON with a list of results.
    Adapt it to your API's result schema.
    """
    try:
        resp = requests.post(ACTUAL_RESULT_API_URL, json={"period": period}, timeout=API_TIMEOUT)
        api_data = resp.json()
        # Try to find the actual result digit (0-9) from the API
        # You must adapt this logic to your real API response!
        # Example: look for a field named 'openCode' or similar
        actual = None
        # Try to find a field with the actual result
        for key in api_data:
            val = api_data[key]
            # If it's a digit in string or int, and only one value, this may be the result
            if isinstance(val, str) and val.isdigit() and len(val) == 1:
                actual = int(val)
            elif isinstance(val, int) and 0 <= val <= 9:
                actual = val
            elif isinstance(val, list):
                # If it's a list with one item that's a digit
                for item in val:
                    if isinstance(item, str) and item.isdigit() and len(item) == 1:
                        actual = int(item)
                        break
                    elif isinstance(item, int) and 0 <= item <= 9:
                        actual = item
                        break
            if actual is not None:
                break
        return actual
    except Exception as e:
        return None

def auto_fetch_results():
    """
    Every 60 seconds, fetch results from the API for all periods with no actual_result.
    """
    while True:
        now = time.time()
        for period, entry in list(period_results.items()):
            # Only check if no actual_result, and prediction is < 30min old, and not already checked
            if entry['actual_result'] is None and now - entry['timestamp'] < 1800 and not entry.get('real_result_checked', False):
                actual = fetch_actual_result_for_period(period)
                if actual is not None:
                    entry['actual_result'] = int(actual)
                    entry['real_result_checked'] = True
                    entry['timestamp'] = now
                    period_results[period] = entry
        time.sleep(60)

threading.Thread(target=auto_fetch_results, daemon=True).start()

# ========== MAIN ==========

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8000, debug=True)
