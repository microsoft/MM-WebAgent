import json
import re

def validate_parsed_plan(planner_response: str, check_content=True, debug=False):
    def if_key_in_all_items(key, item_list):
        return sum(1 for item in item_list if key in item) == len(item_list)

    def if_value_in_all_items(key, valid_values, item_list):
        return sum(1 for item in item_list if str(item[key]) in valid_values) == len(item_list)

    def if_path_exists_in_all_items(planner_json, debug=False):
        prompt = planner_json["code_generation"][0]["prompt"]
        match = re.findall(
            r'["\']?(assets[^\s"\']*\.(?:png|mp4|html))["\']?',
            prompt,
            flags=re.IGNORECASE
        )

        all_paths = set(match)
        save_paths = set()
        for k, v in planner_json.items():
            save_paths_list = [item["save_path"] for item in v if "save_path" in item]
            save_paths.update(save_paths_list)

        success = (save_paths == all_paths)
        if debug and not success:
            print(f"Path validation failed. Expected: {len(all_paths)}, Found: {len(save_paths)}")
            print("Extra found paths:", save_paths.difference(all_paths))
            print("Missing expected paths:", all_paths.difference(save_paths))
        return success

    success = False
    try:
        planner_json = json.loads(planner_response)

        if check_content:
            code_generation = planner_json.get("code_generation", [])
            web_serach_valid = if_key_in_all_items("query", planner_json.get("web_search_knowledge", []))
            code_gen_valid = bool(code_generation) and if_key_in_all_items("prompt", code_generation)
            img_gen_valid = if_key_in_all_items("prompt", planner_json.get("image_generation", [])) \
                            and if_key_in_all_items("save_path", planner_json.get("image_generation", [])) \
                            and if_key_in_all_items("size", planner_json.get("image_generation", []))
            video_gen_valid = if_key_in_all_items("prompt", planner_json.get("video_generation", [])) \
                            and if_key_in_all_items("save_path", planner_json.get("video_generation", [])) \
                            and if_key_in_all_items("seconds", planner_json.get("video_generation", [])) \
                            and if_key_in_all_items("size", planner_json.get("video_generation", [])) \
                            and if_value_in_all_items("seconds", ['4', '8', '12'], planner_json.get("video_generation", [])) \
                            and if_value_in_all_items("size", ['720x1280', '1280x720', '1024x1792', '1792x1024'], planner_json.get("video_generation", []))
            data_vis_valid = if_key_in_all_items("prompt", planner_json.get("data_visualization", [])) \
                            and if_key_in_all_items("save_path", planner_json.get("data_visualization", [])) \
                            and if_key_in_all_items("source_data", planner_json.get("data_visualization", []))
            
            success = web_serach_valid and code_gen_valid and img_gen_valid and video_gen_valid and data_vis_valid
            
            if debug:
                print(f"Validation results - Web Search: {web_serach_valid}, Code Gen: {code_gen_valid}, Image Gen: {img_gen_valid}, Video Gen: {video_gen_valid}, Data Vis: {data_vis_valid}")
                if not video_gen_valid:
                    print("Video generation validation failed.")
                    print(planner_json.get("video_generation", []))

            if success:
                path_valid = if_path_exists_in_all_items(planner_json, debug=debug)
                success = success and path_valid
        else:
            success = True

    except Exception:
        planner_json = {}

    return success, planner_json
