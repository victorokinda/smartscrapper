import hashlib
import re
from typing import List, Optional, Tuple, Dict, Any
from urllib.parse import urljoin, urlparse, parse_qs, urlencode
from bs4 import BeautifulSoup, Tag
from app.models import Record, Attachment, AttachmentStatus, RecordStage, ExtractionPlan


def clean_text(text: Optional[str]) -> str:
    if not text:
        return ""
    return re.sub(r"\s+", " ", text).strip()


def extract_template_signature(html: str) -> str:
    soup = BeautifulSoup(html[:50000], "html.parser")
    # Structural signature based on tag hierarchy and class fingerprints
    tags = [t.name for t in soup.find_all(True)[:100] if t.name not in ("script", "style", "meta", "link")]
    return hashlib.sha256("".join(tags).encode()).hexdigest()[:16]


def is_document_url(url: str, text: str = "") -> bool:
    lower_url = url.lower()
    lower_text = text.lower()
    doc_exts = (".pdf", ".docx", ".doc", ".xlsx", ".xls", ".csv", ".txt", ".rtf", ".odt")
    if any(lower_url.endswith(ext) or ext in lower_url for ext in doc_exts):
        return True
    if any(lower_text.endswith(ext) for ext in doc_exts):
        return True
    if "system/files" in lower_url or "/download" in lower_url or "/webform/public_comments/" in lower_url:
        return True
    return False


class HtmlParser:
    @staticmethod
    def parse_carb_page(html: str, base_url: str, page_number: int) -> Tuple[List[Record], Optional[str]]:
        soup = BeautifulSoup(html, "html.parser")
        records: List[Record] = []
        next_page_url: Optional[str] = None

        # Look for table
        table = soup.find("table")
        if table:
            rows = table.find_all("tr")
            for idx, tr in enumerate(rows[1:]):  # skip header
                tds = tr.find_all("td")
                if len(tds) < 5:
                    continue
                name = clean_text(tds[0].get_text())
                subject = clean_text(tds[1].get_text())
                submitted_to = clean_text(tds[2].get_text())
                file_td = tds[3]
                comm_td = tds[4]

                # Extract detail link
                detail_url = None
                comm_a = comm_td.find("a")
                if comm_a and comm_a.get("href"):
                    detail_url = urljoin(base_url, comm_a["href"])

                # Stable record id
                rec_id = ""
                if detail_url:
                    match = re.search(r"/submissions/(\d+)", detail_url)
                    if match:
                        rec_id = f"carb_{match.group(1)}"
                if not rec_id:
                    # Try file path
                    for a in file_td.find_all("a"):
                        m = re.search(r"/public_comments/(\d+)/", a.get("href", ""))
                        if m:
                            rec_id = f"carb_{m.group(1)}"
                            break
                if not rec_id:
                    rec_id = f"carb_p{page_number}_r{idx}"

                # Extract attachments
                attachments: List[Attachment] = []
                for a_idx, a in enumerate(file_td.find_all("a")):
                    href = a.get("href")
                    if href:
                        full_url = urljoin(base_url, href)
                        orig_name = clean_text(a.get_text()) or full_url.split("/")[-1]
                        att_id = f"{rec_id}_att_{a_idx}"
                        attachments.append(Attachment(
                            attachment_id=att_id,
                            original_name=orig_name,
                            url=full_url,
                            download_status=AttachmentStatus.PENDING
                        ))

                record = Record(
                    record_id=rec_id,
                    source_url=base_url,
                    page_number=page_number,
                    title=subject,
                    submitter=name,
                    proceeding=submitted_to,
                    listing_description=f"Subject: {subject} | Submitter: {name} | Proceeding: {submitted_to}",
                    detail_url=detail_url,
                    stage=RecordStage.LISTING,
                    attachments=attachments
                )
                records.append(record)

        # Pagination
        # In CARB: <a class="pager__link pager__link--next" href="?entity_id=41096&amp;page=1" rel="next"...>
        next_a = soup.find("a", rel="next") or soup.find("a", class_=re.compile(r"pager__link--next"))
        if next_a and next_a.get("href"):
            next_page_url = urljoin(base_url, next_a["href"])

        return records, next_page_url

    @staticmethod
    def parse_carb_detail(html: str, base_url: str) -> Tuple[str, List[Attachment]]:
        soup = BeautifulSoup(html, "html.parser")
        # Extract main text
        for s in soup(["script", "style", "nav", "footer", "header"]):
            s.decompose()
        
        main = soup.find("main") or soup.find("article") or soup.find("div", class_=re.compile(r"content|main"))
        text = clean_text(main.get_text() if main else soup.get_text())

        # Look for any file links on detail page
        attachments: List[Attachment] = []
        for a_idx, a in enumerate(soup.find_all("a")):
            href = a.get("href")
            if href and is_document_url(href, a.get_text()):
                full_url = urljoin(base_url, href)
                orig_name = clean_text(a.get_text()) or full_url.split("/")[-1]
                attachments.append(Attachment(
                    attachment_id=f"detail_att_{a_idx}",
                    original_name=orig_name,
                    url=full_url,
                    download_status=AttachmentStatus.PENDING
                ))
        return text, attachments

    @staticmethod
    def parse_generic_listing(html: str, base_url: str, page_number: int, plan: Optional[ExtractionPlan] = None) -> Tuple[List[Record], Optional[str]]:
        soup = BeautifulSoup(html, "html.parser")
        records: List[Record] = []
        next_page_url: Optional[str] = None

        # Check for CARB first
        if "approved-comments" in base_url or (soup.find("table") and any("Submitted to" in th.get_text() for th in soup.find_all("th"))):
            return HtmlParser.parse_carb_page(html, base_url, page_number)

        # Generic table parsing
        table = soup.find("table")
        if table:
            headers = [clean_text(th.get_text()).lower() for th in table.find_all("th")]
            rows = table.find_all("tr")
            
            # Map column indices from headers
            title_col = None
            submitter_col = None
            for col_i, h in enumerate(headers):
                if any(k in h for k in ("subject", "title", "topic")):
                    title_col = col_i
                elif any(k in h for k in ("name", "submitter", "author")):
                    submitter_col = col_i

            for idx, tr in enumerate(rows):
                tds = tr.find_all("td")
                if not tds:
                    continue
                row_text = [clean_text(td.get_text()) for td in tds]
                links = tr.find_all("a")

                if title_col is not None and title_col < len(row_text):
                    title = row_text[title_col]
                else:
                    title = row_text[0] if len(row_text) > 0 else ""

                if submitter_col is not None and submitter_col < len(row_text):
                    submitter = row_text[submitter_col]
                else:
                    submitter = row_text[1] if len(row_text) > 1 else ""

                desc = " | ".join(row_text)

                detail_url = None
                attachments: List[Attachment] = []
                rec_id = f"rec_p{page_number}_{idx}"

                for a_idx, a in enumerate(links):
                    href = a.get("href")
                    if not href:
                        continue
                    full_href = urljoin(base_url, href)
                    text = clean_text(a.get_text())
                    if is_document_url(full_href, text):
                        attachments.append(Attachment(
                            attachment_id=f"{rec_id}_att_{a_idx}",
                            original_name=text or full_href.split("/")[-1],
                            url=full_href,
                            download_status=AttachmentStatus.PENDING
                        ))
                    elif not detail_url and ("detail" in full_href.lower() or "view" in text.lower() or "submission" in full_href.lower() or a == links[0]):
                        detail_url = full_href

                record = Record(
                    record_id=rec_id,
                    source_url=base_url,
                    page_number=page_number,
                    title=title,
                    submitter=submitter,
                    listing_description=desc,
                    detail_url=detail_url,
                    stage=RecordStage.LISTING,
                    attachments=attachments
                )
                records.append(record)

        # Generic list / card parsing if no table found
        if not records:
            candidate_cards = soup.find_all(class_=re.compile(r"card|item|entry|submission-card|comment-card|record-item"))
            if not candidate_cards:
                candidate_cards = soup.find_all(class_=re.compile(r"submission|comment|result"))
            if not candidate_cards:
                candidate_cards = soup.find_all("article")

            # Keep only items that are not ancestors of other candidates
            cards = [c for c in candidate_cards if not any(child != c and child in c.descendants for child in candidate_cards)]
            for idx, card in enumerate(cards):
                h = card.find(["h1", "h2", "h3", "h4", "a"])
                title = clean_text(h.get_text()) if h else f"Item {idx}"
                desc = clean_text(card.get_text()[:400])
                rec_id = f"card_p{page_number}_{idx}"

                detail_url = None
                attachments = []
                for a_idx, a in enumerate(card.find_all("a")):
                    href = a.get("href")
                    if not href:
                        continue
                    full_href = urljoin(base_url, href)
                    t = clean_text(a.get_text())
                    if is_document_url(full_href, t):
                        attachments.append(Attachment(
                            attachment_id=f"{rec_id}_att_{a_idx}",
                            original_name=t or full_href.split("/")[-1],
                            url=full_href,
                            download_status=AttachmentStatus.PENDING
                        ))
                    elif not detail_url:
                        detail_url = full_href

                record = Record(
                    record_id=rec_id,
                    source_url=base_url,
                    page_number=page_number,
                    title=title,
                    listing_description=desc,
                    detail_url=detail_url,
                    stage=RecordStage.LISTING,
                    attachments=attachments
                )
                records.append(record)

        # Generic pagination
        next_link = soup.find("a", rel="next") or soup.find("a", class_=re.compile(r"next|pagination__next"))
        if not next_link:
            for a in soup.find_all("a"):
                t = clean_text(a.get_text()).lower()
                if "next" in t or "›" in t or "»" in t:
                    next_link = a
                    break
        if next_link and next_link.get("href"):
            next_page_url = urljoin(base_url, next_link["href"])

        return records, next_page_url

    @staticmethod
    def parse_generic_detail(html: str, base_url: str) -> Tuple[str, List[Attachment]]:
        soup = BeautifulSoup(html, "html.parser")
        for s in soup(["script", "style", "nav", "footer", "header"]):
            s.decompose()
        main = soup.find("main") or soup.find("article") or soup.find(class_=re.compile(r"content|detail|submission"))
        desc = clean_text(main.get_text() if main else soup.get_text())

        # Collect attachments near the bottom or anywhere in the document
        attachments: List[Attachment] = []
        for a_idx, a in enumerate(soup.find_all("a")):
            href = a.get("href")
            if href and is_document_url(href, a.get_text()):
                full_url = urljoin(base_url, href)
                t = clean_text(a.get_text()) or full_url.split("/")[-1]
                attachments.append(Attachment(
                    attachment_id=f"detail_att_{a_idx}",
                    original_name=t,
                    url=full_url,
                    download_status=AttachmentStatus.PENDING
                ))
        return desc, attachments

    @staticmethod
    def build_annotated_dom_skeleton(html: str) -> Dict[str, Any]:
        soup = BeautifulSoup(html[:60000], "html.parser")
        for tag in soup(["script", "style", "svg", "noscript", "iframe"]):
            tag.decompose()
        
        candidates = []
        node_id = 0
        # Check tables
        for t in soup.find_all("table"):
            node_id += 1
            headers = [clean_text(th.get_text()) for th in t.find_all("th")]
            sample_rows = []
            for r in t.find_all("tr")[1:3]:
                sample_rows.append([clean_text(td.get_text())[:50] for td in r.find_all("td")])
            candidates.append({
                "candidate_id": f"table_{node_id}",
                "tag": "table",
                "headers": headers,
                "sample_rows": sample_rows,
                "type": "table"
            })

        # Check repeated containers (cards/lists)
        for container_tag in ["ul", "ol", "div"]:
            for parent in soup.find_all(container_tag):
                children = parent.find_all(recursive=False)
                if len(children) >= 3:
                    node_id += 1
                    samples = [clean_text(c.get_text())[:80] for c in children[:2]]
                    candidates.append({
                        "candidate_id": f"list_{node_id}",
                        "tag": container_tag,
                        "class": parent.get("class"),
                        "child_count": len(children),
                        "sample_items": samples,
                        "type": "repeated_list"
                    })
                    break

        # Check pagination candidates
        pager_links = []
        for a in soup.find_all("a"):
            t = clean_text(a.get_text()).lower()
            if "next" in t or "page" in t or a.get("rel") == ["next"]:
                pager_links.append({"text": clean_text(a.get_text()), "href": a.get("href")})
        
        return {
            "candidates": candidates[:10],
            "pagination_candidates": pager_links[:5]
        }
