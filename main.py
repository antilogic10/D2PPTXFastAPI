from fastapi import FastAPI, HTTPException, File, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
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
DOMAIN_NAME = "https://d2pptxfastapi.onrender.com/" #os.getenv("DOMAIN_NAME", "http://localhost:8000/")
os.makedirs(UPLOAD_DIR, exist_ok=True)

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
        </style>
    </head>
    <body>
        <div class="card">
            <h1>🚀 {app.title} is Live</h1>
            <p>All APIs are up and working correctly.</p>
            <p class="uptime">Uptime: {uptime}</p>
            <p>
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

    # Check empty or repeated values (with exception for numeric placeholders)
    for k, v in cleaned_json.items():
        if not v:
            print("\n-----\nEmpty value for key:", k, "\n-----\n")
            return False

        # Reject only if value is literally just the placeholder text
        # AND it's not purely numeric (like "01")
        if v == k and not k.isdigit():
            print("\n-----\nRepeated placeholder text for key:", k, "\n-----\n")
            return False

    return True

@app.post("/generate-ppt")
def generate_ppt(req: PPTRequest):
    # Step 1: Download template
    pptx_path = download_pptx(req.fileUrl)
    textBoxList = list_text_boxes(pptx_path, 0)

    prompt = f"""You are an expert PowerPoint slide content generator.

    If you cannot produce a valid mapping for ALL placeholders, 
    you MUST return ONLY this JSON:
    {{"error": "Content too short for the template. Please provide more detailed content."}}

    Inputs:
    Content: {req.content}
    Placeholders: {json.dumps(textBoxList, indent=2)}

    Given the Placeholders and the image as the context for the slide, I have provided some unstructured content. Your task is to generate a json object with keys as the exact placeholder text and values as the content to fill in those placeholders.
    Guidelines:
    1. Ensure that the content you generate is relevant to the provided content. It should not have any hallucinated or made-up information.Or any information from the image template it is only for reference.
    2. If placeholders with type "list", return a JSON array of strings (each string = one bullet point). If it's plain text, provide a string.
    3. If you cannot find suitable content for a placeholder stop all execution and return {{"error": "Content too short for the template. Please provide more detailed content."}}
    4. Ensure the JSON is properly formatted as the placeholder provided.
    5. Do not include any explanations or additional text outside the JSON. 
    """

    uploadedFile = client.files.upload(file=download_image(req.imageUrl))
    response = client.models.generate_content(
        model="gemini-2.0-flash",
        contents=[prompt,uploadedFile]
    )
    cleanedJson = json.loads((response.text.strip("`")).replace("json","",1).strip())
    print("\n------\nGenerated JSON:", cleanedJson,"\n------\n")
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

        # Build file URL
        file_url = f"{DOMAIN_NAME}{UPLOAD_DIR}/{filename}"
        saved_files.append({"filename": filename, "url": file_url})

    return {"uploaded": saved_files}
