import re
import asyncio
from typing import Optional, Tuple, List, Dict, Any
from urllib.parse import urlparse, parse_qs

from app.models import Record, Attachment, AttachmentStatus, RecordStage
from app.network import NetworkClient


class EUFeedbackPortal:
    """
    Dedicated handler for European Commission 'Have your say' / Better Regulation
    public consultation and feedback initiatives.
    Directly leverages the EU BRP REST endpoints to extract consultation submissions,
    metadata, and attachments cleanly and reliably without relying solely on client-side rendering.
    """

    BASE_URL = "https://ec.europa.eu/info/law/better-regulation"

    @classmethod
    def is_eu_feedback_url(cls, url: str) -> bool:
        if not url:
            return False
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        path = parsed.path.lower()
        if "europa.eu" not in host:
            return False
        return (
            "better-regulation" in path or
            "have-your-say" in path or
            "/initiatives" in path or
            "/brpapi" in path or
            "/allfeedback" in path
        )

    @classmethod
    def extract_ids_from_url(cls, url: str) -> Tuple[Optional[int], Optional[int]]:
        parsed = urlparse(url)
        query = parse_qs(parsed.query)

        pub_id = None
        if "p_id" in query and query["p_id"]:
            try:
                pub_id = int(query["p_id"][0])
            except ValueError:
                pass
        elif "publicationId" in query and query["publicationId"]:
            try:
                pub_id = int(query["publicationId"][0])
            except ValueError:
                pass

        init_id = None
        init_match = re.search(r"/initiatives/(\d+)", parsed.path)
        if init_match:
            try:
                init_id = int(init_match.group(1))
            except ValueError:
                pass
        elif "groupInitiativeId" in query and query["groupInitiativeId"]:
            try:
                init_id = int(query["groupInitiativeId"][0])
            except ValueError:
                pass

        return init_id, pub_id

    @classmethod
    async def fetch_feedbacks(
        cls,
        url: str,
        net_client: NetworkClient,
        page_number: int = 0,
        page_size: int = 10,
        cancel_event: Optional[asyncio.Event] = None
    ) -> Tuple[List[Record], Optional[str], str]:
        """
        Fetches consultation feedback items for an EU initiative.
        Returns:
            (records, next_page_url, initiative_title)
        """
        init_id, pub_id = cls.extract_ids_from_url(url)
        initiative_title = ""

        # If publication ID not directly in URL, query groupInitiatives API to find active publication
        if not pub_id and init_id:
            group_url = f"{cls.BASE_URL}/brpapi/groupInitiatives/{init_id}"
            resp = await net_client.fetch(group_url, cancel_event)
            group_data = resp.json()
            initiative_title = group_data.get("shortTitle", "")
            publications = group_data.get("publications", [])

            # Select publication: open feedback preferred, or latest with totalFeedback > 0, or most recent
            selected_pub = None
            for p in reversed(publications):
                if p.get("receivingFeedbackStatus") == "OPEN":
                    selected_pub = p
                    break
            if not selected_pub:
                for p in reversed(publications):
                    if p.get("totalFeedback", 0) > 0:
                        selected_pub = p
                        break
            if not selected_pub and publications:
                selected_pub = publications[-1]

            if selected_pub:
                pub_id = selected_pub.get("id")

        if not pub_id:
            raise ValueError(f"Could not determine EU publication ID for URL: {url}")

        # Check if page number was passed in URL query
        parsed = urlparse(url)
        query = parse_qs(parsed.query)
        if "page" in query and query["page"]:
            try:
                page_number = int(query["page"][0])
            except ValueError:
                pass

        api_url = f"{cls.BASE_URL}/api/allFeedback?publicationId={pub_id}&page={page_number}&size={page_size}&language=EN"
        resp = await net_client.fetch(api_url, cancel_event)
        data = resp.json()

        items = data.get("content", []) or data.get("myFeedback", [])
        total_pages = data.get("totalPages", 1)
        is_last = data.get("last", True)

        records: List[Record] = []
        for item in items:
            fb_id = item.get("id")
            first_name = (item.get("firstName") or "").strip()
            surname = (item.get("surname") or "").strip()
            submitter_name = f"{first_name} {surname}".strip() or item.get("userType") or "Anonymous"
            org = item.get("organization")
            feedback_text = (item.get("feedback") or "").strip()
            country = item.get("country", "")

            # Construct stable record ID
            rec_id = f"eu_{pub_id}_{fb_id}"
            
            # Format listing description with key metadata
            desc_parts = []
            if feedback_text:
                desc_parts.append(feedback_text)
            if country:
                desc_parts.append(f"[Country: {country}]")
            if item.get("userType"):
                desc_parts.append(f"[User Type: {item.get('userType')}]")
            listing_desc = " ".join(desc_parts)

            # Detail URL for viewing on Have your say
            init_slug = f"/initiatives/{init_id}" if init_id else ""
            detail_url = f"{cls.BASE_URL}/have-your-say{init_slug}/feedback_en?p_id={pub_id}#feedback-{fb_id}"

            # Attachments
            attachments: List[Attachment] = []
            for a_idx, att_raw in enumerate(item.get("attachments", [])):
                # Note: EU download API requires the documentId (e.g. 090166e534e9cc65) or attachment ID
                doc_id = str(att_raw.get("documentId") or att_raw.get("id"))
                file_name = att_raw.get("fileName") or att_raw.get("ersFileName") or f"document_{doc_id}.pdf"
                att_url = f"{cls.BASE_URL}/api/download/{doc_id}"
                attachments.append(Attachment(
                    attachment_id=f"{rec_id}_att_{a_idx}",
                    original_name=file_name,
                    url=att_url,
                    download_status=AttachmentStatus.PENDING
                ))

            record = Record(
                record_id=rec_id,
                source_url=url,
                page_number=page_number,
                title=f"Feedback on {initiative_title}" if initiative_title else f"EU Feedback {fb_id}",
                submitter=submitter_name,
                organization=org,
                proceeding=f"EU Initiative {init_id} (Pub {pub_id})" if init_id else f"EU Publication {pub_id}",
                listing_description=listing_desc,
                detail_url=detail_url,
                stage=RecordStage.LISTING,
                attachments=attachments
            )
            records.append(record)

        # Pagination
        next_page_url = None
        if not is_last and (page_number + 1 < total_pages):
            next_page_url = f"{cls.BASE_URL}/api/allFeedback?publicationId={pub_id}&page={page_number + 1}&size={page_size}&language=EN"

        return records, next_page_url, initiative_title
