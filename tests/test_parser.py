import pytest
from app.parser import HtmlParser, is_document_url, clean_text, extract_template_signature
from app.models import RecordStage, AttachmentStatus


SAMPLE_CARB_HTML = """
<!DOCTYPE html>
<html>
<head><title>Approved Comments</title></head>
<body>
<table>
  <thead>
    <tr>
      <th>Name</th>
      <th>Subject:</th>
      <th>Submitted to:</th>
      <th>File Upload (i.e., Attachment):</th>
      <th>Comment</th>
    </tr>
  </thead>
  <tbody>
    <tr>
      <td>David Perry</td>
      <td>Senate Bill (SB) 253 and SB 261</td>
      <td>Public Comments to California Climate-Disclosure</td>
      <td>N/A</td>
      <td><a href="/form/public-comments/submissions/19491">View</a></td>
    </tr>
    <tr>
      <td>Katherine Markova</td>
      <td>Re: Information Solicitation SB 253</td>
      <td>Public Comments to California Climate-Disclosure</td>
      <td>
        <a href="/system/files/webform/public_comments/19781/Response_SB253.pdf">Response_SB253.pdf</a>
        <a href="/system/files/webform/public_comments/19781/Appendix_Data.xlsx">Appendix_Data.xlsx</a>
      </td>
      <td><a href="/form/public-comments/submissions/19781">View</a></td>
    </tr>
  </tbody>
</table>
<nav class="pager">
  <ul class="pager__items">
    <li class="pager__item pager__item--next">
      <a class="pager__link pager__link--next" href="?entity_id=41096&amp;page=1" rel="next">Next page››</a>
    </li>
  </ul>
</nav>
</body>
</html>
"""

SAMPLE_EU_LISTING_HTML = """
<!DOCTYPE html>
<html>
<body>
<div class="submission-list">
  <div class="submission-card">
    <h3><a href="/submissions/csrd-101">Feedback from Global Reporting Initiative</a></h3>
    <p class="summary">Short summary of CSRD sustainability reporting standards feedback.</p>
  </div>
  <div class="submission-card">
    <h3><a href="/submissions/csrd-102">Feedback from Individual Citizen</a></h3>
    <p class="summary">Personal opinion on disclosure mandates.</p>
  </div>
</div>
<a class="pagination__next" href="/consultation?page=2">Next &raquo;</a>
</body>
</html>
"""

SAMPLE_EU_DETAIL_HTML = """
<!DOCTYPE html>
<html>
<body>
<article class="submission-detail">
  <h1>Feedback from Global Reporting Initiative</h1>
  <div class="main-content">
    <p>This is the detailed full submission statement covering CSRD interoperability and auditing requirements across Europe.</p>
    <p>We represent international sustainability standard setters...</p>
  </div>
  <div class="attachments-section" style="margin-top: 50px;">
    <h4>Attached Documents</h4>
    <a href="/download/csrd_gri_response_final.pdf">GRI CSRD Official Response.pdf</a>
    <a href="/download/opaque_file_endpoint?doc=992">Download Submission File</a>
  </div>
</article>
</body>
</html>
"""


def test_carb_table_parsing():
    records, next_page = HtmlParser.parse_carb_page(
        SAMPLE_CARB_HTML,
        base_url="https://ww2.arb.ca.gov/approved-comments?entity_id=41096&page=0",
        page_number=0
    )
    assert len(records) == 2
    assert next_page == "https://ww2.arb.ca.gov/approved-comments?entity_id=41096&page=1"

    # Row 1: No attachment
    r1 = records[0]
    assert r1.record_id == "carb_19491"
    assert r1.submitter == "David Perry"
    assert "253" in r1.title and "261" in r1.title
    assert len(r1.attachments) == 0
    assert r1.detail_url == "https://ww2.arb.ca.gov/form/public-comments/submissions/19491"

    # Row 2: Two attachments
    r2 = records[1]
    assert r2.record_id == "carb_19781"
    assert len(r2.attachments) == 2
    assert r2.attachments[0].original_name == "Response_SB253.pdf"
    assert r2.attachments[1].original_name == "Appendix_Data.xlsx"


def test_eu_style_listing_and_detail():
    records, next_page = HtmlParser.parse_generic_listing(
        SAMPLE_EU_LISTING_HTML,
        base_url="https://consultation.europa.eu/csrd",
        page_number=0
    )
    assert len(records) == 2
    assert next_page == "https://consultation.europa.eu/consultation?page=2"
    assert records[0].detail_url == "https://consultation.europa.eu/submissions/csrd-101"

    # Detail page extraction
    detail_desc, attachments = HtmlParser.parse_generic_detail(
        SAMPLE_EU_DETAIL_HTML,
        base_url="https://consultation.europa.eu/submissions/csrd-101"
    )
    assert "GRI" in detail_desc or "sustainability standard setters" in detail_desc
    assert len(attachments) == 2
    assert attachments[0].original_name == "GRI CSRD Official Response.pdf"
    # Opaque file endpoint detection
    assert attachments[1].original_name == "Download Submission File"
    assert "opaque_file_endpoint" in attachments[1].url


def test_opaque_and_document_url_detection():
    assert is_document_url("https://example.com/file.pdf")
    assert is_document_url("https://example.com/report.docx")
    assert is_document_url("https://example.com/system/files/download/12345")
    assert is_document_url("https://example.com/api/download?id=123", text="Download PDF")
    assert not is_document_url("https://example.com/about-us")
    assert not is_document_url("https://example.com/logo.png")


def test_dom_skeleton_generation():
    skeleton = HtmlParser.build_annotated_dom_skeleton(SAMPLE_CARB_HTML)
    assert "candidates" in skeleton
    assert len(skeleton["candidates"]) >= 1
    table_cand = skeleton["candidates"][0]
    assert table_cand["type"] == "table"
    assert "Name" in table_cand["headers"]
    assert len(skeleton["pagination_candidates"]) >= 1
