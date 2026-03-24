import re
import json

def parse_score(res, mode="v1", debug=False):
    success = True
    parsed_results = {}
    if mode == "v1":
        penalty = re.search(r'Total Penalty[^0-9]*([0-9]+(?:\.[0-9]+)?)', res, re.DOTALL)
        penalty = float(penalty.group(1)) if penalty else -1

        success = False if penalty == -1 else success
        total_score = penalty
        parsed_results["score"] = total_score
    elif mode == "v2":
        aspects = ["Layout", "Typography", "Color", "Clarity", "Professional"]

        # strict score set
        allowed_scores = {"0.1", "0.5", "1.0", "1.5", "2.0", "0.2", "0.4", "0.6", "0.8"}
        pattern = re.compile(
            rf"^({'|'.join(aspects)}):\s*([0-9]+\.[0-9]+)", 
            re.IGNORECASE | re.MULTILINE
        )

        matches = pattern.findall(res)
        if matches:
            scores_dict = {}
            total_score = 0.0
            valid = True

            for aspect, score_str in matches:
                score_str = score_str.strip()

                # validate score
                if score_str not in allowed_scores:
                    valid = False
                    continue

                score_val = float(score_str)
                scores_dict[aspect] = score_val
                total_score += score_val

            # must contain all 5 aspects AND all scores valid
            if len(scores_dict) == len(aspects) and valid:
                total_score /= len(aspects)
                success = True
            else:
                total_score, success = -1, False
        else:
            total_score, success = -1, False

        parsed_results["score"] = total_score

    elif mode == "v3":
        id = re.search(r'meta_design[^0-9]*([0-9]+).*?reasoning', res, re.DOTALL)

        id = int(id.group(1)) if id else None
        total_score = re.search(r'final_score[^0-9]*([0-9]+(?:\.[0-9]+)?)', res, re.DOTALL)
        total_score = float(total_score.group(1)) if total_score else -1

        success = False if total_score == -1 else success
        parsed_results["parsed_info"] = id
        parsed_results["score"] = total_score

    elif mode == "v5":
        total_score = re.search(r'final_score[^0-9]*([0-9]+(?:\.[0-9]+)?)', res, re.DOTALL)
        total_score = float(total_score.group(1)) if total_score else -1
        suggestions = {}
        global_sugg = re.search(r'global:\s*(.*?)\s*subimage:', res, re.DOTALL)
        global_sugg = global_sugg.group(1).strip() if global_sugg else None
        subimage_sugg = re.search(r'subimage:\s*(.*)final_score', res, re.DOTALL)
        subimage_sugg = subimage_sugg.group(1).strip() if subimage_sugg else None
        success = False if total_score == -1 else success
        parsed_results["parsed_info"] = {"global": global_sugg, "subimage": subimage_sugg}
        parsed_results["score"] = total_score

    elif mode == "v4":
        results = json.loads(res.strip("```json").strip("```").strip())
        parsed_results["parsed_info"] = results
        parsed_results["score"] = results.get("score", None)
        success = True

    elif mode == "none":
        pass
    
    else:
        raise ValueError("Unsupported mode")

    return parsed_results, success
    

def get_issues_from_penalties(text):
    issues = re.findall(r"-\s*([^:]+):\s*Penalty--\d+", text)
    issues = "\n".join([f"- {issue.strip()}" for issue in issues])
    return issues
