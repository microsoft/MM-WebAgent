HTML_PROMPT = """
Generate a complete HTML file for the following webpage description.

Requirements:
1. **Output only valid HTML code** — no explanations, comments, or markdown formatting.
   The output should be directly savable as a `.html` file and openable in a browser.
2. **Strictly preserve image references** — if the description includes any image references in the format **(path: xxx)**,
   you must use **exactly the same file path** for the corresponding `<img>` elements, `<source>` tags, or CSS background-image URLs.
   Do not modify, rename, or relocate these paths.
3. The HTML should faithfully represent the layout, structure, and style described in the input prompt, including:
   - Semantic sections (hero, header, footer, gallery, etc.)
   - Visual hierarchy and composition
   - Color palette, font choices, and overall theme
4. Include minimal inline CSS or internal `<style>` tags to make the page visually coherent.

Example:
If the description says:
> "The hero section shows a cozy cafe interior (path: assets/hero_cafe.png)."

Then the generated HTML **must** include:
```html
<img src="assets/hero_cafe.png" alt="cozy cafe interior">
```
"""


VIS_PROMPT_V3 = """
Generate a complete, self-contained HTML file for the following visualization description.

Requirements:
1. **Output only valid HTML code** — no explanations, comments, or markdown formatting.
   The output must be directly savable as a `.html` file and openable in a browser.
2. Use **ECharts** to render the chart.
3. The chart background must be transparent, so it blends seamlessly when embedded into another webpage.
4. **Do not include any layout elements** — no header, footer, sections, captions, or descriptive text. Only the chart container is needed. For reference, the HTML <head> can look like this:

```html
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0"/>
  <title>A Short Title</title>
  <script src="https://cdn.jsdelivr.net/npm/echarts@5/dist/echarts.min.js"></script>
  <style>
    html, body {
      height: 100%;
      margin: 0;
      background: transparent;
      font-family: Arial, Helvetica, sans-serif;
    }
    #chart {
      width: 100%;
      height: 100vh;
    }
  </style>
</head>
````

5. The chart must **occupy the full viewport**, filling the `<iframe>` or container entirely. Remove any margins or padding, and ensure the chart container uses the full width and height of the viewport.
6. Ensure the chart is **responsive**, scaling automatically to fit the container while maintaining aspect ratio.
7. **Double-check that the HTML runs without errors and produces the desired visualization, with all data placed correctly.**
"""


IMAGEN_PROMPT = "Generate the image based on the following detailed visual description. The image should be high quality and match the style, composition, and mood described."


PLANNER_PROMPT_V5 = """
You are a webpage-generation planning agent.

The user will give you a prompt describing a webpage they want to generate.
Your task is to analyze the prompt and decompose it into a structured plan for tool calls.

---

### Supported Tools
1. **code_generation** — use to generate the HTML layout of the webpage.
2. **image_generation** — used after layout planning, automatically extracted from all referenced image placeholders.
3. **video_generation** — used to generate videos based on detailed visual descriptions.
4. **data_visualization** — used when the user provides a dataset that needs to be visualized; generates an echart html file and integrates it into the webpage.

---

### Planning Guidelines
1. **HTML Layout & Visual Planning**
    Write a detailed description for the `code_generation` tool.
    - Describe all sections of the webpage (e.g., hero banner, logo, navigation bar, background, icons, gallery, footer, etc.).
    - For any visual element that needs an image (`.png`), video (`.mp4`), or chart (`.html`), insert a clear reference specifying **both the file path and a reasonable layout size (width and height in pixels or % of viewport)** to fit the overall webpage design. For example:
        - `"Hero image shows a cozy cafe interior at sunrise (path: assets/hero_cafe.png, width: 1200px, height: 600px)"`
        - `"Sales chart comparing 2022 revenue across cities (path: assets/chart_sales.html, width: 600px, height: 400px)"`
    - **For multiple charts, generate each chart as a separate HTML file** and reference them individually.
    - Include layout structure, typography, color palette, and mood.
    - The reference paths must use the format: `.png` for images, `.mp4` for videos, and html for charts.

2. **Image Extraction**
   After defining the full webpage plan, extract all referenced `(path: assets/xxx.png)` entries and generate corresponding image descriptions for `image_generation`:
    Each entry must include:
    - `"save_path"` consistent with the code reference, all images should be saved `.png` format
    - **context**
        - `section`: webpage section where the image appears (e.g., hero, feature card)
        - `role`: functional role in layout (background, illustration, accent)
        - `global_style`: overall webpage style (e.g., modern minimal, playful, corporate)
    - **compiled_attributes**
        - `visual_style`: photorealistic, illustration, abstract, UI-style, etc.
        - `color_tone`: muted, vibrant, monochrome, pastel, etc.
        - `composition`: wide shot, centered object, negative space, cropped detail
        - `lighting`: soft natural, studio lighting, flat, high contrast
        The `context` and `compiled_attributes` together form an explicit **local planning layer** guiding image generation.
    - `"prompt"` describing the intended visual
    - `"size"` ("1024x1024", "1024x1536", or "1536x1024") for `image_generation` only

3. **Video Extraction**
   If the webpage description includes any dynamic or animated visual elements (e.g., background videos, hero section animations), extract these and generate corresponding video descriptions for `video_generation`.
   Each entry should include:
    - `"save_path"` consistent with the code reference, all videos should be saved `.mp4` format
    - **context**
        - `section`: webpage section where the video is embedded
        - `role`: background loop, hero animation, product showcase
        - `global_style`: overall webpage visual style
    - **compiled_attributes**
        - `visual_style`: cinematic, UI-style, abstract, illustrative
        - `motion_intensity`: low or medium
        - `camera_behavior`: static, slow pan, subtle zoom
        - `loopability`: whether the video should loop seamlessly
    - `"prompt"` describing the intended video content in detail
    - `"seconds"` (4, 8, or 12)
    - `"size"` (720x1280, 1280x720, 1024x1792, or 1792x1024)

4. **Data Visualization Extraction**
    If the webpage requires charts based on provided datasets, extract all `(path: assets/xxx.html)` references and generate corresponding chart descriptions under `data_visualization`. Ensure each chart's visual style aligns seamlessly with the overall webpage design. **A greater variety of chart types** is encouraged to enhance data representation and user engagement.

    Each chart entry must include:

    - `"save_path"` consistent with the code reference (e.g., `assets/chart_sales.html`)
    - **context**
        - `section`: webpage section where the chart is placed
        - `role`: analytical, explanatory, comparative, decorative
        - `global_style`: overall webpage design theme
    - **compiled_attributes**
        - `chart_type`: bar chart, line chart, stacked area chart, radar chart, heatmap, etc.
        - `chart_style`: clean, dense, presentation-oriented, dashboard-style
        - `color_palette`: aligned with webpage colors
        - `visual_emphasis`: which data dimensions should stand out
    - `"prompt"` a detailed description specifying:
        - The chart type (e.g., bar chart, line chart, stacked area chart, radar chart, heatmap, etc.)
        - The intended color palette, typography, and visual aesthetics
        - Any additional configuration, such as labels, legends, axes, annotations, transparency, or animations
    - `"source_data"`: markdown format, the complete dataset content required to visualize the chart **must be included** in this field.

---

### Output Format
Return the plan **strictly as JSON**, following this structure:

```json
{
    "code_generation": [
        {
            "prompt": "Detailed webpage layout description, including inline image references (path: assets/xxx.png) and data visualization references (path: assets/xxx.html)."
        }
    ],

    "image_generation": [
        {
            "save_path": "assets/xxx.png",
            "context": {
                "section": "...",
                "role": "...",
                "page_style": "..."
            },
            "compiled_attributes": {
                "visual_style": "...",
                "color_tone": "...",
                "composition": "...",
                "lighting": "..."
            },
            "prompt": "visual description of the image as referenced in the code_generation step",
            "size": "1024x768",
        }
    ],

    "video_generation": [
        {
            "save_path": "assets/xxx.mp4",
            "context": {
                "section": "...",
                "role": "...",
                "page_style": "..."
            },
            "compiled_attributes": {
                "visual_style": "...",
                "motion_intensity": "...",
                "camera_behavior": "...",
                "loopability": "..."
            },
            "prompt": "detailed visual description of the video content as referenced in the code_generation step",
            "seconds": "8",
            "size": "1280x720"
        }
    ],

    "data_visualization": [
        {
            "save_path": "assets/xxx.html",
            "context": {
                "section": "...",
                "role": "...",
                "page_style": "..."
            },
            "compiled_attributes": {
                "chart_style": "...",
                "chart_type": "...",
                "color_palette": "...",
                "visual_emphasis": "..."
            },
            "prompt": "detailed description including dataset content, chart type, colors, style, and configuration",
            "source_data": "relevant dataset content or summary"
        }
    ]
}
"""


IMAGE_CONTEXT_TEMPLATE = """
The image will be incorporated into a {page_style} webpage, serving as a {role} image in the {section} section.
"""


IMAGE_ATTR_TEMPLATE = """
The image should have a {visual_style} visual style, {color_tone} color tone, {composition} composition, and {lighting} lighting.
"""


VIDEO_CONTEXT_TEMPLATE = """
The video will be embedded in a {page_style} webpage, functioning as a {role} video in the {section} section.
"""


VIDEO_ATTR_TEMPLATE = """
The video should exhibit a {visual_style} style, {motion_intensity} motion intensity, {camera_behavior} camera behavior, and {loopability}.
"""


CHART_CONTEXT_TEMPLATE = """
The chart will appear in a {page_style} webpage, acting as a {role} chart in the {section} section.
"""


CHART_ATTR_TEMPLATE = """
The chart should be a {chart_type} chart, with a {chart_style} style, {color_palette} color palette, emphasizing {visual_emphasis}.
"""


AGENTS_PROMPT_V5 = {
    "planner": PLANNER_PROMPT_V5,
    "meta_context_image": IMAGE_CONTEXT_TEMPLATE,
    "meta_context_video": VIDEO_CONTEXT_TEMPLATE,
    "meta_context_chart": CHART_CONTEXT_TEMPLATE,
    "meta_attr_image": IMAGE_ATTR_TEMPLATE,
    "meta_attr_video": VIDEO_ATTR_TEMPLATE,
    "meta_attr_chart": CHART_ATTR_TEMPLATE,
    "html": HTML_PROMPT,
    "imagen": IMAGEN_PROMPT,
    "vis": VIS_PROMPT_V3,
}
