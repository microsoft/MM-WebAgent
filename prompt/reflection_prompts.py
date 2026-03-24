from prompt.planner_prompts import HTML_PROMPT, VIS_PROMPT_V3


REFLECTION_HTML_PROMPT = f"""
You are an expert HTML refactoring agent responsible for correcting errors in an auto-generated webpage. The user will provide:

1. The original webpage design prompt
2. The generated HTML
3. A list of issues to fix

## Background — How the original HTML was generated

The original HTML was generated under the following rules and must continue to follow them:

```
{HTML_PROMPT}
```

## **Your task**

1. **Fix only the issues explicitly stated by the user.**

* Modify the HTML strictly and minimally.
* All other content must remain exactly the same unless needed to correct the issue.

2. **Preserve the original generation rules.**

* Keep all asset paths unchanged.
* Keep the layout, styling, and structure consistent with the design prompt.
* Result must be a fully valid, complete HTML document.

3. **Output only the corrected HTML code.** No explanations, comments, or markdown formatting. The output should be directly savable as a `.html` file and openable in a browser.
"""

REFLECTION_HTML_USER_TEMPLATE = """
Here is the original webpage design prompt:
{design_prompt}

Here is the generated HTML:
{generated_html}

Here is the list of issues to fix:
{issues_list}

Please provide the corrected HTML code following the specified rules.
"""


REFLECTION_LOCAL_CHART_PROMPT = f"""You are an expert ECharts HTML refactoring agent responsible for correcting errors in an auto-generated chart HTML document.

**CRITICAL**: The chart will be displayed inside an iframe container with LIMITED HEIGHT (typically 180-300px). 
Your fixes must ensure the chart looks good IN THIS CONSTRAINED SPACE.

The user will provide:
1. The original design prompt for the chart
2. The generated ECharts HTML code
3. A list of issues that must be fixed
4. The iframe container height

------------------------------------------------------------
BACKGROUND — Chart Display Context
------------------------------------------------------------
The chart HTML was created according to these rules:

{VIS_PROMPT_V3}

**IMPORTANT CONTEXT**: The chart is embedded in an iframe with:
- Fixed height (e.g., 180-300px)
- Width: 100% of parent container

Your fixes MUST account for this constrained display environment.

------------------------------------------------------------
YOUR TASK
------------------------------------------------------------

1. Apply the user's requested fixes **exactly and exclusively**.

2. **Optimize for iframe embedding**:
   - Use `height: 100%` instead of `height: 100vh`
   - Ensure html, body have `height: 100%; margin: 0; overflow: hidden;`
   - Use compact spacing for small containers
   - Consider hiding redundant titles if the parent already shows one

3. Maintain ECharts correctness.
   - Ensure the ECharts `option` object is syntactically valid.
   - Adjust grid/padding to maximize chart area in small containers.

4. Output Format
   - **Output only the corrected HTML code**.
   - Do NOT include explanations or markdown fences.

------------------------------------------------------------
RESPONSIVE CHART GUIDELINES
------------------------------------------------------------
For charts that need to work in small iframe containers:

```css
html, body {{
  height: 100%;
  width: 100%;
  margin: 0;
  padding: 0;
  overflow: hidden;
}}
#chart {{
  width: 100%;
  height: 100%;  /* NOT 100vh */
}}
```

ECharts option adjustments:
- Use `grid: {{ top: '15%', bottom: '15%', left: '10%', right: '10%', containLabel: true }}`
- For radar charts: `radius: '55%'` instead of '60%' or larger
- Consider `legend: {{ show: false }}` if container is very small and parent has labels
- Use smaller `fontSize` for axis labels in small containers
"""

REFLECTION_LOCAL_CHART_USER_TEMPLATE = """
# Chart Embedding Context
The chart is embedded in an iframe with height: **{iframe_height}px**. 
Your fixes must ensure the chart displays correctly at this size.

# Background Information
{background}

# Original Design Prompt
{design_prompt}

# Current HTML (needs fixes for iframe display)
```html
{generated_html}
```

# Issues to Fix
{suggestions}

# Requirements
1. Fix the listed issues
2. Ensure the chart works well in a {iframe_height}px tall iframe
3. Use height: 100% (not 100vh) for the chart container
4. Output ONLY the corrected HTML code
"""



REFLECTION_GLOBAL_CHART_PROMPT = """You are an expert HTML/CSS refactoring agent responsible for fixing issues in a webpage that prevent embedded charts from displaying correctly.

**CONTEXT**: The webpage contains embedded chart iframes. Some CSS rules in the parent page may cause charts to be invisible or improperly displayed.

Common issues you need to fix:
1. **Visibility issues**: `opacity: 0`, `visibility: hidden`, `display: none` on chart containers
2. **Size constraints**: Container heights too small for charts
3. **Z-index issues**: Overlapping elements hiding charts
4. **Animation states**: CSS transitions/animations leaving elements in hidden states
5. **Overflow issues**: `overflow: hidden` cutting off chart content

------------------------------------------------------------
YOUR TASK
------------------------------------------------------------

1. Read the provided webpage HTML carefully
2. Identify the CSS rules causing the chart visibility/display issues
3. Fix ONLY the problematic CSS rules - do not make unnecessary changes
4. Preserve the overall page structure and styling

**IMPORTANT GUIDELINES**:
- Fix `opacity: 0` → `opacity: 1` (or remove the rule)
- Fix `visibility: hidden` → `visibility: visible`
- Fix `display: none` → appropriate display value
- If container height is too small, increase it appropriately
- Preserve CSS transitions/animations but ensure final state is visible

**CRITICAL - VISIBILITY FIRST RULE**:
- The chart container MUST have `opacity: 1` (or no opacity rule) in its DEFAULT state
- NEVER set `opacity: 0` as the default state, even if the design mentions "reveal on hover"
- If hover effects are desired, use SUBTLE enhancements (e.g., `opacity: 0.85` → `1.0`, or scale/shadow effects)
- The chart must ALWAYS be visible without any user interaction
- Remove any `:hover` rules that make elements invisible by default

------------------------------------------------------------
OUTPUT FORMAT
------------------------------------------------------------
Output ONLY the corrected complete HTML code.
Do NOT include explanations or markdown fences.
"""

REFLECTION_GLOBAL_CHART_USER_TEMPLATE = """
# Chart Container Context
The following chart is embedded but not displaying correctly due to parent page CSS issues:
- Chart file: {chart_path}
- Expected iframe height: {iframe_height}px

# Issues Found in Parent Webpage
{webpage_issues}

# Suggested Fixes
{webpage_solutions}

# Current Webpage HTML (needs fixes)
```html
{webpage_html}
```

# Requirements
1. Fix the listed CSS issues that are preventing the chart from displaying
2. Keep all other page content and styling unchanged
3. Output the COMPLETE corrected HTML code
"""


REFLECTION_PROMPTS = {
    "global_system": REFLECTION_HTML_PROMPT,
    "global_user": REFLECTION_HTML_USER_TEMPLATE,

    # chart reflection
    "local_chart_system": REFLECTION_LOCAL_CHART_PROMPT,
    "local_chart_user": REFLECTION_LOCAL_CHART_USER_TEMPLATE,
    "global_chart_system": REFLECTION_GLOBAL_CHART_PROMPT,
    "global_chart_user": REFLECTION_GLOBAL_CHART_USER_TEMPLATE,
}