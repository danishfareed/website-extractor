# WebReconstruct 🚀 - Ultimate Website to Static Site Extractor

**WebReconstruct** is a powerful, SEO-optimized CLI tool that transforms any dynamic website into a lightweight, fully functional static site. It captures everything—from metadata and structured JSON data to every CSS, JS, and Image asset—making website archiving and migration easier than ever.

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.8+](https://img.shields.io/badge/python-3.8+-blue.svg)](https://www.python.org/downloads/)

---

## 🌟 Key Features

- **Full Static Reconstruct**: Generates a complete `website/` folder with local HTML files.
- **Asset Portability**: Automatically downloads all Images, CSS, and JS files.
- **SEO Ready**: Extracts Titles, Meta Descriptions, and Slugs into a structured `data.json`.
- **Intelligent Link Rewriting**: Converts online URLs into local `.html` links for seamless offline navigation.
- **Fast & Async**: Built with `httpx` and `asyncio` for high-performance crawling.
- **Layman Friendly**: No complex configuration required. Just enter a URL and watch it work.

---

## 🚀 Getting Started

Follow these simple steps to start extracting any website on your Mac or Linux machine.

### 1. Installation

First, clone the repository and navigate to the project folder:

```bash
git clone git@github.com:danishfareed/website-extractor.git
cd website-extractor
```

### 2. Setup (Recommended)

Create a virtual environment to keep your system clean:

```bash
python3 -m venv venv
source venv/bin/activate
```

Install the dependencies:

```bash
pip install -r requirements.txt
```

### 3. Run the Extractor

Enter the domain you want to extract:

```bash
python extractor.py https://example.com
```

---

## 📁 Output Structure

Once the extraction is complete, you'll find a new folder (e.g., `example_com_extracted/`) with the following structure:

```text
example_com_extracted/
├── data.json           # Structured JSON for data-driven apps
└── website/            # The fully functional static website
    ├── index.html      # Homepage
    ├── contact.html    # Internal pages
    └── assets/
        ├── css/        # Downloaded stylesheets
        ├── js/         # Downloaded scripts
        └── images/      # Downloaded images
```

---

## 🛠 Tech Stack

- **Python**: The core logic.
- **BeautifulSoup4**: For advanced HTML parsing and rewriting.
- **HTTPX**: A next-generation HTTP client for Python.
- **Rich**: For the beautiful terminal interface and progress tracking.

---

## 🤝 Contributing

Contributions are welcome! Feel free to open an issue or submit a pull request.

---

## 📜 License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.

---

**Made with ❤️ by [Danish Fareed](https://github.com/danishfareed)**
