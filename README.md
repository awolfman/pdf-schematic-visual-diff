Language / Язык: Russian | English
pdf-schematic-visual-diff
An automated Git hook (pre-commit) for visual quality control of changes in PDF schematics and blueprints, designed specifically for hardware development repositories (CAD/EDA) with Git LFS support.
Since recent updates, the project now supports Cadence Allegro / OrCAD Capture netlist parsing and structural validation alongside the pixel-by-pixel visual comparison.
⚡ When the hook triggers: The script executes automatically at the moment of running git commit. It intercepts staged PDF files, extracts data from Git LFS on the fly, and branches its behavior based on the backend mode defined in .schematic-backend. It generates a visual report named [name]_diff.pdf and, if in Cadence mode, validates and updates semantic netlist tracking files before automatically appending them to the current commit.

📂 Project Structure
The project has evolved into a structured suite of specialized tools and configuration states:
project/
├── .git/
│   └── hooks/
│       └── pre-commit              # Primary Git hook entry point
├── tools/
│   ├── pdf-diff.sh                 # Low-level visual PDF comparison engine
│   ├── check_cadence_netlist.sh    # Main pipeline controller for Cadence validation
│   ├── check_capture_export.py     # Verification script for exporter output
│   ├── parse_capture_netlist.py    # Extracts data from OrCAD netlist files
│   └── compare_netlists.py         # Evaluates structural changes between commits
├── netlists/
│   └── cadence.json                # Versioned baseline database of the netlist
├── .schematic-backend              # Operational mode configuration file (pdf / cadence)
└── .gitignore                      # Workspace exclusion rules
⚠️ Important: Do not add the tools/ directory or netlists/cadence.json to your .gitignore. They must remain versioned to maintain consistent automation across environments.

⚙️ Backend Execution Modes
The tool adapts its behavior using the .schematic-backend configuration file. This file must be explicitly added to the index:
git add .schematic-backend
Supported values inside .schematic-backend:
pdf — Standard mode. Performs visual PDF-diff operations only.
cadence — Extended validation mode for Cadence Allegro / OrCAD Capture projects.
🔄 Cadence Execution Pipeline
When set to cadence, the pre-commit hook runs the following sequence sequentially:
PDF-diff: Executes the visual pixel-by-pixel blueprint comparison.
Export Integrity Check: Runs check_capture_export.py to scan netlist.log and .dat files for compiler errors.
Netlist Parsing: Runs parse_capture_netlist.py to process pstchip.dat, pstxprt.dat, and pstxnet.dat structural content.
Baseline Evaluation: Fetches the baseline schematic model from the previous commit history (HEAD).
Semantic Comparison: Runs compare_netlists.py to check the current workspace model against the baseline.
Smart Blocking: The commit is blocked/rejected only if errors or structural ambiguities are detected.
Database Tracking Update: Regenerates and updates the baseline tracking file at netlists/cadence.json.
Automatic Staging: Automatically runs git add netlists/cadence.json to embed the database into the current commit.

✨ Blueprint Color Coding Logic
The algorithm operates at a low level by comparing pixel brightness via vector channel subtraction (-compose MinusSrc), which allows it to accurately detect structural changes in CAD schematics:
Component Removal: All removed symbols/components are highlighted in blue.
Component Addition or RefDes Change: Highlighted in red.
Component Movement: The old position of the component on the schematic turns blue, while the new position turns red.

🚀 Implementation Features
Semantic & Visual Dual-Validation: Combines absolute graphical change catching with electrical netlist topology checking.
Full Git LFS Integration: Automatically detects LFS text pointers in the commit history and safely performs the smudge procedure in an isolated workspace.
Anti-Aliasing Resilience: Hardware-accelerated matrix operations paired with morphological expansion (Dilate Disk:1) capture micro-shifts in 1-pixel thick vector lines, preventing false negatives.
High Performance: Page rendering and pixel processing are completely parallelized using GNU Parallel paired with multi-threaded pdftocairo batch processing. Internal execution boundaries are set to prevent CPU core thrashing.

🔧 Configuring ImageMagick Resource Limits
For processing large CAD documents (such as A1 or A0 blueprints at 300 DPI), the default ImageMagick security parameters might be insufficient, throwing Image width exceeds user limit warnings or drastically slowing down due to hard drive cache thrashing.
Recommended Policy Profiles
Modify your ImageMagick security configuration file (usually located at /etc/ImageMagick-7/policy.xml or /etc/ImageMagick-6/policy.xml).
Add or update the following parameters inside the <policymap> tags right before the closing </policymap> element:
<policymap>
  <!-- Maximum single image raster boundaries (16384 x 16384 pixels) -->
  <policy domain="resource" name="width" value="16KP"/>
  <policy domain="resource" name="height" value="16KP"/>
  
  <!-- Maximum total frame area in megapixels (128 MP) -->
  <policy domain="resource" name="area" value="128MP"/>
  
  <!-- Memory utilization and memory mapping constraints -->
  <policy domain="resource" name="memory" value="2GiB"/>
  <policy domain="resource" name="map" value="4GiB"/>
  
  <!-- Temporary scratch disk limit if RAM allocation fills up -->
  <policy domain="resource" name="disk" value="8GiB"/>
  
  <!-- Execution timeout ceiling for a single command process (10 minutes) -->
  <policy domain="resource" name="time" value="600"/>
</policymap>
📌 Note: Although the pre-commit script dynamically attempts to override and isolate these resource environments for its background processes on the fly, manually editing the global system policy.xml ensures predictable behavior during individual manual debug sessions.

🛠 Dependencies and Packages
Before installing the hook, make sure the following packages are installed on your system:
Script Command
Tool Purpose
OpenSUSE
Debian/Ubuntu/Mint
Fedora/RHEL
git-lfs
Extracts heavy binary PDFs from LFS storage
git-lfs
git-lfs
git-lfs
magick
Accelerated matrix-based image composite operations
ImageMagick
imagemagick
ImageMagick
pdftocairo
Renders PDF vectors into crisp raster PNG graphics
poppler-tools
poppler-utils
poppler-utils
parallel
Distributes batch page workflows across CPU cores
parallel
parallel
parallel
qpdf
Performs quick non-destructive PDF page merging
qpdf
qpdf
qpdf
img2pdf
Assembles raster diff layers without re-compression
python3-img2pdf
img2pdf
img2pdf

Installation by Distribution
OpenSUSE Tumbleweed / Leap 15.4+:
sudo zypper install git git-lfs ImageMagick poppler-tools parallel qpdf python3-img2pdf
Debian 11+ / Ubuntu 22.04+ / Linux Mint 21+:
sudo apt update
sudo apt install git git-lfs imagemagick poppler-utils parallel qpdf img2pdf
Fedora 38+ / RHEL 9+ / AlmaLinux 9+:
sudo dnf install git git-lfs ImageMagick poppler-utils parallel qpdf img2pdf
✅ Installation Verification
for tool in git magick pdftocairo img2pdf parallel qpdf; do
    command -v "$tool" >/dev/null 2>&1 && echo "OK: tool" || echo "MISSING: tool"
done

⚠️ Important: magick (IM 7) vs convert (IM 6)
The script relies on the magick command, which is the native interface for ImageMagick 7. In most distributions, the package manager installs ImageMagick 6, where the same functionality is accessed via the convert command.
If You Have ImageMagick 6
Replace magick with convert inside the script using this command:
sed -i 's/magick/convert/g' .git/hooks/pre-commit

💻 Repository Configuration & Hook Setup
Step 1. Initialize Git LFS for PDFs and CAD Sources
Run the following commands in the root of your repository based on your CAD system:
For Cadence Allegro / OrCAD Capture:
git lfs install
git lfs track "*.pdf" "*.dsn"
git add .gitattributes
For Altium Designer:
git lfs install
git lfs track "*.pdf" "*.SchDoc"
git add .gitattributes
For KiCad:
git lfs install
git lfs track "*.pdf" "*.kicad_sch"
git add .gitattributes
Step 2. Configure Environment and Exclusions for Cadence
When configuring the project for a Cadence tracking pipeline, intermediate netlist text outputs must be configured properly to keep the index clean.
Configure OrCAD Capture to perform its netlist export into a strict service directory inside the project root. The target files inside must have fixed names:
.capture-export/netlist.log
.capture-export/pstchip.dat
.capture-export/pstxprt.dat
.capture-export/pstxnet.dat
Add this service export directory to your .gitignore configuration so the absolute .dat matrices are never indexed:
.capture-export/
Initialize tracking for all fundamental configuration boundaries, baseline JSON states, and helper tooling scripts:
git add .schematic-backend .gitignore tools netlists/cadence.json
🚨 Critical Limitation: The current integration validation script (check_capture_export.py) cannot programmatically guarantee that the generated .dat structures correspond to the absolute last saved state of the primary .dsn project file. Therefore, before running any git commit inside a Cadence setup, you must explicitly trigger the OrCAD Capture Netlist Export into the .capture-export directory right before creating the commit.

🔧 Troubleshooting
All Pages Marked as "Modified" Though the Schematic Hasn't Changed
This is caused by differing poppler rendering behavior between PDF versions or font embedding discrepancies. Check your files using pdfinfo:
pdfinfo old.pdf | grep -E "Pages|Page size"
If the page dimensions match but full-page false positives persist, try lowering the sensitivity by increasing the THRESHOLD value via environmental variables before running your commit:
export PDF_DIFF_THRESHOLD="2%"
git commit -m "Commit message"
💡 Important when using CUPS-PDF (Linux): This printer converts all text directly into vector curves (graphical paths). For a visual diff tool, this is the ideal scenario because letter shapes are frozen as geometry, removing any dependency on local system font packages.
⚠️ Critical Limitations:
Both compared versions must be generated via CUPS-PDF using identical format and scaling settings.
It is highly recommended to maintain the exact same export pipeline, including the host application and Wine. Updating the system driver or Ghostscript can also alter the rendering output.
Comparing results from different virtual printers is unsupported. It may highlight a significant part of the page even if the source schematic has not changed.
The hook compares the purely visual appearance of the pages, not the electrical connections or component semantics. Any visual discrepancy is potentially flagged as a modification.
Commit Takes Too Long to Process
By default, the script renders at DPI=300. For large Multi-sheet A1 layouts, this can be heavy. Lower the comparison resolution down to 150 or 200 DPI:
export PDF_DIFF_DPI=150
export PDF_DIFF_OUTPUT_DPI=150
git commit -m "Commit with faster diff calculation"
This resolution remains fully sufficient for tracking components and Reference Designators (RefDes).

🐳 CI/Docker Integration
If you run this hook inside a CI/CD environment, you can utilize a pre-built Docker image configuration:
FROM python:3.12-slim

RUN apt-get update && apt-get install -y     git git-lfs imagemagick poppler-utils parallel     && rm -rf /var/lib/apt/lists/*

RUN pip install img2pdf

# Substitute magick with convert for ImageMagick 6 environments
RUN sed -i 's/magick/convert/g' /usr/local/bin/pre-commit-hook
Example GitLab CI workflow configuration:
stages:
  - validate

visual-diff:
  stage: validate
  image: your-registry/pdf-diff-runner:latest
  script:
    - git lfs install
    - git lfs pull
    - .git/hooks/pre-commit
  only:
    - merge_requests

🔒 Security and Privacy
Zero Telemetry. The script never transmits data to external servers.
NDA Compliant. Schematics and their corresponding diff reports reside strictly within your repository ecosystem.
👥 Authors & AI Contributors
awolfman — Project Concept, Hook Logic, Bash Implementation, and Hardware CAD/EDA Integration Testing
DeepSeek — Optimization of Low-Level FX Math, Linux Package Diagnostics, and Memory Leak (RAM Spikes) Protections
Claude — Parallelization Strategy (GNU Parallel Infrastructure) and Anti-Aliasing Resilience Operations
ChatGPT — CI/CD Integration Architecture, Docker Environment Deployment, and Troubleshooting Resolution Logic
Gemini (Google AI) — Technical Documentation Refinement, English Localization, and Bilingual Layout Structuring
