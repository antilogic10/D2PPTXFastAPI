from fastapi import FastAPI, HTTPException
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

app = FastAPI()

# Allow all origins
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # <-- allows all origins
    allow_credentials=True,
    allow_methods=["*"],  # <-- allows all methods (GET, POST, etc.)
    allow_headers=["*"],  # <-- allows all headers
)

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
    text_boxes = []
    for shape in slide.shapes:
        if shape.has_text_frame:
            shape.text and text_boxes.append(shape.text)
    return text_boxes   

def updateTemplatePlaceholders(pptx_path: str, slide_index: int, replacements: dict):
    prs = Presentation(pptx_path)
    slide = prs.slides[slide_index]
    for shape in slide.shapes:
        if shape.has_text_frame:
            for paragraph in shape.text_frame.paragraphs:
                for run in paragraph.runs:
                    if run.text in replacements:
                        print("Replacing:", run.text, "->", replacements[run.text])
                        run.text = replacements[run.text]

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


@app.post("/generate-ppt")
def generate_ppt(req: PPTRequest):
    # Step 1: Download template
    pptx_path = download_pptx(req.fileUrl)

    prompt = f"""You are an expert PowerPoint slide content generator.  

    You are given three inputs:  
    1. An image of the PowerPoint template (for layout only — DO NOT read or use any text from this image).  
    2. Unstructured user-provided content.  
    3. An array of placeholder keys (representing text boxes in the template).  
    
    Your task:  
    - ONLY use the provided content to generate text for each placeholder.  
    - DO NOT use, read, or copy any text from the template image.  
    - If the provided content is longer than the number of placeholders, summarize it clearly and concisely to fit.  
    - DO NOT add or invent (hallucinate) any new information not present in the content.  
    - Preserve the meaning of the user content while improving clarity and structure.  
    - Do not change the text content unless it exceeds the placeholder length by 15 characters.
    - Ensure the generated text fits naturally within the context of a PowerPoint presentation.
    - The output MUST be a **valid JSON object** where:  
       - Keys = placeholder array values.  
       - Values = generated content for that placeholder.  
    - Do not return anything else except the JSON object.  
    - Emphasizing again - DO NOT use any text from the template image.
    
    Inputs: 
    Content: {req.content}  
    Placeholders: {list_text_boxes(pptx_path, 0)}  
    """
    uploadedFile = client.files.upload(file=download_image(req.imageUrl))
    response = client.models.generate_content(
        model="gemini-2.0-flash",
        contents=[prompt,uploadedFile]
    )
    cleanedJson = json.loads((response.text.strip("`")).replace("json","",1).strip())
    # print("\n",req.content,"\n")
    print("Generated JSON:", cleanedJson)
    # Step 2: Replace placeholders
    updated_pptx =updateTemplatePlaceholders(pptx_path, 0, cleanedJson)

    # Step 3: Return file
    filename = "updated_presentation.pptx"
    return FileResponse(updated_pptx, media_type="application/vnd.openxmlformats-officedocument.presentationml.presentation", filename=filename)
