from fastapi import FastAPI, HTTPException, File, UploadFile
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
import os
from pptx import Presentation
from google import genai
import json
import requests
import tempfile
from datetime import datetime
from fastapi.middleware.cors import CORSMiddleware
import uuid
from fastapi.staticfiles import StaticFiles
import shutil
from typing import List


UPLOAD_DIR = "uploaded_files"
GENERATED_DIR = "generated_files"
DOMAIN_NAME = os.getenv("DOMAIN_NAME", "http://localhost:8000")
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.umask(0o022)

app = FastAPI()

# Allow all origins
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # <-- allows all origins
    allow_credentials=True,
    allow_methods=["*"],  # <-- allows all methods (GET, POST, etc.)
    allow_headers=["*"],  # <-- allows all headers
)

app.mount(f"/{GENERATED_DIR}", StaticFiles(directory=GENERATED_DIR), name="files")
# Serve uploaded files
app.mount(f"/{UPLOAD_DIR}", StaticFiles(directory=UPLOAD_DIR), name="files")
# Pydantic model for request body
class PPTRequest(BaseModel):
    fileUrl: str   # Name of the pptx template file
    content: str  # Unstructured content to be filled in the pptx
    imageUrl: str   # image url uploaded to gemini for context
    rewriteWithAi: bool = False  # Whether to rewrite content with AI
# Initialize Gemini client
client = genai.Client(api_key=os.getenv("GEMINI_API"))

def list_text_boxes(pptx_path: str, slide_index: int):
    prs = Presentation(pptx_path)
    slide = prs.slides[slide_index]
    placeholders = {}

    for shape in slide.shapes:
        if shape.has_text_frame and shape.text.strip():
            # Check if any paragraph is bulleted
            is_list = any(p.level > 0 or p.text.strip().startswith("•") for p in shape.text_frame.paragraphs)

            # Use the first non-empty run text as the "placeholder key"
            placeholder_key = shape.text.strip()

            if is_list:
                items = [p.text.strip() for p in shape.text_frame.paragraphs if p.text.strip()]
                placeholders[placeholder_key] = {"type": "list", "items": items}
            else:
                placeholders[placeholder_key] = {"type": "text", "value": shape.text.strip()}

    return placeholders


def updateTemplatePlaceholders(pptx_path: str, slide_index: int, replacements: dict):
    prs = Presentation(pptx_path)
    slide = prs.slides[slide_index]

    for shape_idx, shape in enumerate(slide.shapes):
        if shape.has_text_frame:
            original_text = shape.text.strip()
            if original_text in replacements:
                if isinstance(replacements[original_text], dict) and "value" in replacements[original_text]:
                    new_value = replacements[original_text]["value"]
                else:
                    new_value = replacements[original_text]
                
                # Detect if template shape is a list
                is_list_shape = (
                    len(shape.text_frame.paragraphs) > 1
                    or any(p.level > 0 for p in shape.text_frame.paragraphs)
                    or any(p._pPr is not None and p._pPr.xpath(".//a:buChar") for p in shape.text_frame.paragraphs)
                    or any(p._pPr is not None and p._pPr.xpath(".//a:buAutoNum") for p in shape.text_frame.paragraphs)
                )

                # 🔹 If template expects a list but Gemini returned string → wrap in list
                if is_list_shape and isinstance(new_value, str):
                    new_value = [new_value]

                # 🔹 If template expects plain text but Gemini returned list → join
                if not is_list_shape and isinstance(new_value, list):
                    new_value = " ".join(new_value)

                # --- Replace text based on detected type ---
                if isinstance(new_value, str):
                    for paragraph in shape.text_frame.paragraphs:
                        for run in paragraph.runs:
                            if run.text.strip() == original_text:
                                run.text = new_value
                
                elif isinstance(new_value, type(None)):
                    for paragraph in shape.text_frame.paragraphs:
                        for run in paragraph.runs:
                            if run.text.strip() == original_text:
                                run.text = ""

                elif isinstance(new_value, list):
                    paragraphs = shape.text_frame.paragraphs

                    counter = 0
                    for item in new_value:
                        if counter < len(paragraphs):
                            # ✅ Replace only the text of the first run, preserve formatting
                            if paragraphs[counter].runs:
                                paragraphs[counter].runs[0].text = item
                                # Clear out extra runs if any
                                for r in paragraphs[counter].runs[1:]:
                                    r.text = ""
                            else:
                                paragraphs[counter].text = item
                        else:
                            # ✅ If template doesn't have enough list items, add new ones
                            p = shape.text_frame.add_paragraph()
                            p.text = item
                            p.level = 0
                        counter += 1

                    # ✅ Clear any extra template bullets beyond what Gemini gave
                    for p in paragraphs[counter:]:
                        if p.runs:
                            for r in p.runs:
                                r.text = ""
                        else:
                            p.text = ""

                else:
                    print(f"Skipping unknown type for {original_text}")

    output_path = tempfile.NamedTemporaryFile(delete=False, suffix=".pptx").name
    prs.save(output_path)
    return output_path


def download_pptx(url: str) -> str:
    # """Download PPTX from the given URL and save locally"""
    response = requests.get(url)
    if response.status_code != 200:
        raise HTTPException(status_code=400, detail="Could not download PPT file")
    
    tmp_file = tempfile.NamedTemporaryFile(delete=False, suffix=".pptx")
    tmp_file.write(response.content)
    tmp_file.close()
    return tmp_file.name

def download_image(url: str) -> str:
    # """Download image from the given URL and save locally"""
    response = requests.get(url)
    ext=url.split('.')[-1] if '.' in url else ''

    if response.status_code != 200:
        raise HTTPException(status_code=400, detail="Could not download image file")
    
    tmp_file = tempfile.NamedTemporaryFile(delete=False, suffix=f".{ext}")
    tmp_file.write(response.content)
    tmp_file.close()
    return tmp_file.name

STARTED_AT = datetime.utcnow()

@app.get("/",response_class=HTMLResponse)
def home():
    uptime = datetime.utcnow() - STARTED_AT
    
    # 🧮 Get Disk Usage
    total, used, free = shutil.disk_usage("/")
    used_gb = used / (2**30)
    total_gb = total / (2**30)
    free_gb = free / (2**30)
    percent_used = (used / total) * 100

    # 🧱 Return HTML
    return f"""
    <!doctype html>
    <html lang="en">
    <head>
        <meta charset="utf-8"/>
        <title>{app.title} • Status</title>
        <style>
            body {{
                font-family: system-ui, sans-serif;
                background: #0f172a;
                color: #f1f5f9;
                display: flex;
                justify-content: center;
                align-items: center;
                height: 100vh;
                margin: 0;
            }}
            .card {{
                background: #1e293b;
                padding: 2rem 3rem;
                border-radius: 1rem;
                text-align: center;
                box-shadow: 0 10px 20px rgba(0,0,0,0.5);
                width: 400px;
            }}
            h1 {{
                margin: 0 0 0.5rem;
                font-size: 1.8rem;
                color: #38bdf8;
            }}
            p {{ margin: 0.5rem 0; color: #cbd5e1; }}
            .uptime {{
                font-size: 0.9rem;
                color: #94a3b8;
            }}
            a {{
                color: #38bdf8;
                text-decoration: none;
            }}
            a:hover {{ text-decoration: underline; }}
            .progress {{
                background: #334155;
                border-radius: 10px;
                overflow: hidden;
                height: 16px;
                width: 100%;
                margin: 10px 0;
                box-shadow: inset 0 1px 3px rgba(0,0,0,0.3);
            }}
            .progress-bar {{
                height: 100%;
                background: linear-gradient(90deg, #38bdf8, #0ea5e9);
                width: {percent_used:.2f}%;
                transition: width 0.5s ease-in-out;
            }}
            .disk-info {{
                font-size: 0.85rem;
                color: #a1a1aa;
            }}
        </style>
    </head>
    <body>
        <div class="card">
            <h1>🚀 {app.title} is Live</h1>
            <p>All APIs are up and working correctly.</p>
            <p class="uptime">Uptime: {uptime}</p>

            <div style="margin-top:1rem;">
                <p>💾 Disk Usage</p>
                <div class="progress">
                    <div class="progress-bar"></div>
                </div>
                <p class="disk-info">
                    Used: {used_gb:.2f} GB / {total_gb:.2f} GB<br/>
                    Free: {free_gb:.2f} GB ({100 - percent_used:.2f}%)
                </p>
            </div>

            <p style="margin-top:1rem;">
                <a href="/docs">Interactive Docs</a> • 
                <a href="/redoc">ReDoc</a>
            </p>
        </div>
    </body>
    </html>
    """

def validateJson(cleaned_json, textBoxList):
    # Check for explicit error
    if "error" in cleaned_json:
        print("\n-----\nError found in JSON:", cleaned_json["error"], "\n-----\n")
        return False

    # Check placeholder mismatch
    if len(cleaned_json.keys()) != len(textBoxList):
        print("\n-----\nPlaceholder count mismatch:", len(cleaned_json.keys()), "vs", len(textBoxList), "\n-----\n")
        return False

    # Validate values
    seen_values = set()
    for k, v in cleaned_json.items():
        if not v:
            print("\n-----\nEmpty value for key:", k, "\n-----\n")
            return False

        # Skip numeric placeholders (like 01, 02…)
        if k.isdigit():
            continue

        # Disallow "Heading 3": "Heading 3" or "Slide title": "Slide title"
        if isinstance(v, str) and v.strip().lower() == k.strip().lower():
            print("\n-----\nRepeated value for key:", k, "\n-----\n")
            return False

        # Handle unhashable types safely
        if isinstance(v, (dict, list)):
            v_hash = json.dumps(v, sort_keys=True)  # Convert to string for hashing
        else:
            v_hash = str(v).strip()

        # Optional: disallow duplicate non-numeric values
        if v_hash in seen_values:
            print("\n-----\nDuplicate value detected:", v, "\n-----\n")
            return False
        seen_values.add(v_hash)

    return True

@app.post("/generate-ppt")
def generate_ppt(req: PPTRequest):
    # Step 1: Download template
    pptx_path = download_pptx(req.fileUrl)
    textBoxList = list_text_boxes(pptx_path, 0)

    prompt = f""" """
    
    if(req.rewriteWithAi):
        prompt = f"""You are an expert PowerPoint content writer and designer.

        Your task is to:
        1. Rewrite and enhance the provided content so it is polished, professional, and suitable for business presentations.
        2. Map the rewritten content accurately to the given placeholders.
        3. Ensure the rewritten text fits **visually and contextually** within the layout bounds of each placeholder based on the provided slide image.

        If you cannot produce a valid mapping for every placeholder,
        return only this JSON:
        {{"error": "Content too short for the template. Please provide more detailed content."}}

        ---

        ### Inputs
        - Content: {req.content}
        - Placeholders: {json.dumps(textBoxList, indent=2)}
        - Slide Layout Image: The image visually represents the slide’s structure and placeholder spacing. Use it only to gauge **content density and balance**, not design elements or colors.

        ---

        ### Core Guidelines

        1. **Rewriting Rules**
           - Rewrite the content in a professional, concise, and presentation-friendly tone.
           - Preserve the **original meaning and factual accuracy**.
           - You **may** lightly expand or enrich the content with factual, contextually relevant insights **only** if:
             - There is sufficient visual space in the corresponding placeholder, and
             - The addition improves slide clarity or flow.
           - Avoid verbosity — aim for **balanced, slide-fitting text**.
           - Do **not** add new unrelated facts, data, or visuals.

        2. **Layout Awareness**
           - Infer approximate text limits per placeholder by analyzing the slide layout.
           - Prefer short sentences, bullet points, and crisp phrasing.
           - Titles and headings should be brief (ideally ≤ 6 words).
           - Lists should have 3–5 concise bullets at most.
           - Body text should not overflow beyond visible box limits.

        3. **Mapping Logic**
           - Assign each rewritten content segment to the placeholder that best fits its intent (title, subtitle, list, etc.).
           - For placeholders marked `"list"`, output an array of bullet strings.
           - For `"text"` placeholders, output a single concise string.
           - Ensure every placeholder is meaningfully filled without redundancy.

        4. **Validation**
           - If the content is clearly too short to fill all placeholders,
             return:
             {{"error": "Content too short for the template. Please provide more detailed content."}}
           - Never include partial mappings, commentary, markdown, or any non-JSON text.

        ---

        ### Output Format
        - Return **one valid JSON object only**.
        - Keys = exact placeholder text.
        - Values = strings or string arrays according to type.
        - Output must be **valid, compact JSON**, properly closed, and free of any extra formatting.
        """
    else:
        prompt = f"""You are an expert PowerPoint content generator. 
        Your task is to map the provided content to the given placeholders so it fits naturally within the slide layout.

        If you cannot produce a valid mapping for every placeholder,
        return only this JSON:
        {{"error": "Content too short for the template. Please provide more detailed content."}}

        ### Inputs
        - Content: {req.content}
        - Placeholders: {json.dumps(textBoxList, indent=2)}

        ### Core Rules
        1. **Never fabricate, hallucinate, or invent** any information.
        2. Use the content **exactly as provided** — no paraphrasing, shortening, or expansion.
           (This prompt assumes the content should be used as-is.)
        3. The slide image is only a visual reference. Do **not** use its design, text, or colors for content inference.

        ### Mapping Logic
        1. Analyze the placeholders and identify their roles (e.g., title, heading, subheading, body text, list, etc.).
        2. Identify logical segments in the content — such as paragraphs, newlines, punctuation, colons, dashes, or bullet-like structures.
        3. Map each segment to the placeholder that best fits it:
           - Match titles or overarching themes to placeholders like “Slide Title” or “Title”.
           - Match short phrases or step names to placeholders like “Heading 1”, “Heading 2”, etc.
           - Match longer text or multiple points to “list” placeholders.
        4. If a placeholder’s type is `"list"`, return a JSON array of strings (each string = one bullet point).
           If a placeholder’s type is `"text"`, return a single string.
        5. Use all provided content meaningfully and distribute it logically across placeholders.
        6. If the content clearly does not contain enough distinct segments to fill all placeholders,
           return only:
           {{"error": "Content too short for the template. Please provide more detailed content."}}

        ### Output Requirements
        - Return a **single valid JSON object**.
        - Keys = exact placeholder text from the provided list.
        - Values = strings or string arrays as per placeholder type.
        - Do not include explanations, notes, or markdown formatting.
        - The response must be **strictly valid JSON** with no extra text before or after it.
        """

    uploadedFile = client.files.upload(file=download_image(req.imageUrl))
    response = client.models.generate_content(
        model="gemini-2.0-flash",
        contents=[prompt,uploadedFile]
    )
    cleanedJson = json.loads((response.text.strip("`")).replace("json","",1).strip())
    print("\n----- Prompted ----",prompt,"\n---end prompt---","\n------\nGenerated JSON:", cleanedJson,"\n------\n")
    if not validateJson(cleanedJson, textBoxList):
        if os.path.exists(pptx_path):
            os.remove(pptx_path)
        return {"error": "Error Generating PPTX, Content too short for the template. Please provide more detailed content."}
    else:
        updated_pptx =updateTemplatePlaceholders(pptx_path, 0, cleanedJson)

        # Step 3: Generate unique filename
        unique_id = uuid.uuid4().hex[:8]  # short UUID
        public_filename = f"presentation_{unique_id}.pptx"
        public_path = os.path.join(GENERATED_DIR, public_filename)

        # Move to public folder
        shutil.copy(updated_pptx, public_path)
        # make the file publicly readable
        os.chmod(public_path, 0o755)
        # Delete the temporary file
        if os.path.exists(updated_pptx):
            os.remove(updated_pptx)

        if os.path.exists(pptx_path):
            os.remove(pptx_path)

        # Step 4: Return public URL
        file_url = f"{DOMAIN_NAME}{GENERATED_DIR}/{public_filename}"
        return {"file_url": file_url}

@app.post("/upload-files/")
async def upload_files(files: List[UploadFile] = File(...)):
    saved_files = []

    for file in files:
        # Always use the original filename
        filename = file.filename
        file_path = os.path.join(UPLOAD_DIR, filename)

        # "wb" mode automatically replaces file if it already exists
        with open(file_path, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)

        # Make the file publicly readable
        os.chmod(file_path, 0o755)

        # Build file URL
        file_url = f"{DOMAIN_NAME}{UPLOAD_DIR}/{filename}"
        saved_files.append({"filename": filename, "url": file_url})

    return {"uploaded": saved_files}
