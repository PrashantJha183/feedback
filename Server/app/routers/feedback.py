from fastapi import APIRouter, HTTPException
from app.models.feedback import Feedback
from app.models.user import User
from app.models.feedback_request import FeedbackRequest
from app.models.notification import Notification
from app.schemas.feedback import (
    FeedbackCreate, FeedbackOut, CommentIn, ExportPDFResponse, FeedbackRequestIn
)
from datetime import datetime
from typing import List
import markdown2
import io
from reportlab.pdfgen import canvas
from fastapi.responses import StreamingResponse

router = APIRouter()

# -----------------------------
# Create Feedback (Manager to Employee)
# -----------------------------
@router.post("/", response_model=FeedbackOut)
async def create_feedback(payload: FeedbackCreate):
    mgr = await User.find_one(
        User.employee_id == payload.manager_employee_id,
        User.role == "manager"
    )
    if not mgr:
        raise HTTPException(404, "Manager not found")

    employee = await User.find_one(
        User.employee_id == payload.employee_id,
        User.role == "employee"
    )
    if not employee:
        raise HTTPException(404, "Employee not found")

    fb = Feedback(
        employee_id=payload.employee_id,
        manager_employee_id=payload.manager_employee_id,
        strengths=payload.strengths,
        improvement=payload.improvement,
        sentiment=payload.sentiment,
        anonymous=payload.anonymous,
        tags=payload.tags or [],
        acknowledged=False,
        comments=[],
        created_at=datetime.utcnow()
    )
    await fb.insert()

    await Notification(
        employee_id=payload.employee_id,
        manager_employee_id=mgr.employee_id,
        manager_name=mgr.name,
        message=f"You have received new feedback from manager {mgr.name}"
    ).insert()

    return FeedbackOut.from_feedback(fb, mgr.name)


# -----------------------------
# Employee Requests Feedback
# -----------------------------
@router.post("/request")
async def request_feedback(payload: FeedbackRequestIn):
    emp = await User.find_one(
        User.employee_id == payload.employee_id,
        User.role == "employee"
    )
    if not emp:
        raise HTTPException(404, "Employee not found")

    mgr = await User.find_one(
        User.employee_id == payload.manager_employee_id,
        User.role == "manager"
    )
    if not mgr:
        raise HTTPException(404, "Manager not found")

    fr = FeedbackRequest(
        employee_id=payload.employee_id,
        manager_employee_id=payload.manager_employee_id,
        message=payload.message,
        seen=False,
        created_at=datetime.utcnow()
    )
    await fr.insert()

    await Notification(
        employee_id=payload.manager_employee_id,
        manager_employee_id=payload.manager_employee_id,
        manager_name=mgr.name,
        message=f"Feedback request from employee {payload.employee_id}"
    ).insert()

    return {"message": "Feedback request submitted successfully"}


# -----------------------------
# Get All Feedback Requests for Manager
# -----------------------------
@router.get("/requests/{manager_id}")
async def get_feedback_requests(manager_id: str):
    mgr = await User.find_one(
        User.employee_id == manager_id,
        User.role == "manager"
    )
    if not mgr:
        raise HTTPException(404, "Manager not found")

    requests = await FeedbackRequest.find(
        FeedbackRequest.manager_employee_id == manager_id
    ).sort("-created_at").to_list()

    return [
        {
            "id": str(req.id),
            "employee_id": req.employee_id,
            "message": req.message,
            "seen": req.seen,
            "created_at": req.created_at
        }
        for req in requests
    ]


# -----------------------------
# Mark Feedback Request as Seen
# -----------------------------
@router.patch("/requests/{request_id}/seen")
async def mark_feedback_request_seen(request_id: str):
    req = await FeedbackRequest.get(request_id)
    if not req:
        raise HTTPException(404, "Feedback request not found")

    req.seen = True
    await req.save()
    return {"message": "Feedback request marked as seen"}


# -----------------------------
# Count Unseen Requests for Manager
# -----------------------------
@router.get("/requests/{manager_id}/count-unseen")
async def count_unseen_requests(manager_id: str):
    mgr = await User.find_one(
        User.employee_id == manager_id,
        User.role == "manager"
    )
    if not mgr:
        raise HTTPException(404, "Manager not found")

    count = await FeedbackRequest.find(
        FeedbackRequest.manager_employee_id == manager_id,
        FeedbackRequest.seen == False
    ).count()

    return {"unseen_count": count}


# -----------------------------
# View Feedback History (Employee)
# -----------------------------
@router.get("/employee/{employee_id}", response_model=List[FeedbackOut])
async def get_feedback_history(employee_id: str):
    fbs = await Feedback.find(Feedback.employee_id == employee_id).to_list()
    out = []
    for fb in fbs:
        mgr = await User.find_one(User.employee_id == fb.manager_employee_id)
        comments_html = [
            {"employee_id": c["employee_id"], "text": markdown2.markdown(c["text"])}
            for c in getattr(fb, "comments", [])
        ]
        out.append(FeedbackOut.from_feedback(
            fb,
            mgr.name if mgr else "Unknown",
            comments_html
        ))
    return out


# -----------------------------
# Acknowledge Feedback
# -----------------------------
@router.patch("/acknowledge/{feedback_id}")
async def acknowledge(feedback_id: str):
    fb = await Feedback.get(feedback_id)
    if not fb:
        raise HTTPException(404, "Feedback not found")

    fb.acknowledged = True
    await fb.save()

    mgr = await User.find_one(User.employee_id == fb.manager_employee_id)
    if mgr:
        await Notification(
            employee_id=fb.manager_employee_id,
            manager_employee_id=fb.manager_employee_id,
            manager_name=mgr.name,
            message=f"Employee {fb.employee_id} acknowledged your feedback."
        ).insert()

    return {"message": "Feedback acknowledged"}


# -----------------------------
# Update Feedback (Manager only)
# -----------------------------
@router.put("/{feedback_id}", response_model=FeedbackOut)
async def update_feedback(feedback_id: str, upd: FeedbackCreate):
    fb = await Feedback.get(feedback_id)
    if not fb:
        raise HTTPException(404, "Feedback not found")

    mgr = await User.find_one(
        User.employee_id == upd.manager_employee_id,
        User.role == "manager"
    )
    if not mgr or fb.manager_employee_id != mgr.employee_id:
        raise HTTPException(403, "Not authorized")

    fb.strengths = upd.strengths
    fb.improvement = upd.improvement
    fb.sentiment = upd.sentiment
    fb.tags = upd.tags or []
    fb.anonymous = upd.anonymous
    await fb.save()

    return FeedbackOut.from_feedback(fb, mgr.name)


# -----------------------------
# Delete Feedback
# -----------------------------
@router.delete("/{feedback_id}")
async def delete_feedback(feedback_id: str):
    fb = await Feedback.get(feedback_id)
    if not fb:
        raise HTTPException(404, "Feedback not found")

    mgr = await User.find_one(
        User.employee_id == fb.manager_employee_id,
        User.role == "manager"
    )
    if not mgr:
        raise HTTPException(403, "Not authorized")

    await fb.delete()
    return {"message": "Deleted"}


# -----------------------------
# Delete All Feedback by Manager
# -----------------------------
@router.delete("/manager/{manager_id}")
async def delete_all(manager_id: str):
    mgr = await User.find_one(
        User.employee_id == manager_id,
        User.role == "manager"
    )
    if not mgr:
        raise HTTPException(403, "Not authorized")

    deleted = await Feedback.find(
        Feedback.manager_employee_id == manager_id
    ).delete()
    return {"message": f"Deleted {deleted} items"}


# -----------------------------
# Add Comment to Feedback
# -----------------------------
@router.post("/comment/{feedback_id}")
async def comment(feedback_id: str, comment: CommentIn):
    fb = await Feedback.get(feedback_id)
    if not fb:
        raise HTTPException(404, "Feedback not found")

    emp = await User.find_one(User.employee_id == comment.employee_id)
    if not emp or emp.role != "employee":
        raise HTTPException(403, "Not authorized")

    fb.comments = getattr(fb, "comments", [])
    fb.comments.append({
        "employee_id": comment.employee_id,
        "text": comment.text
    })
    await fb.save()

    mgr = await User.find_one(User.employee_id == fb.manager_employee_id)
    if mgr:
        await Notification(
            employee_id=fb.manager_employee_id,
            manager_employee_id=fb.manager_employee_id,
            manager_name=mgr.name,
            message=f"Employee {comment.employee_id} commented on your feedback."
        ).insert()

    return {"message": "Comment added"}


# -----------------------------
# Export Feedback as PDF
# -----------------------------
@router.get("/export/{employee_id}", response_model=ExportPDFResponse)
async def export_pdf(employee_id: str):
    fbs = await Feedback.find(Feedback.employee_id == employee_id).to_list()
    buf = io.BytesIO()
    p = canvas.Canvas(buf)
    p.drawString(100, 800, f"Feedback Report for Employee ID: {employee_id}")
    y = 780
    for fb in fbs:
        p.drawString(
            100,
            y,
            f"{fb.sentiment.upper()} - {fb.strengths} | {fb.improvement}"
        )
        y -= 20
        if y < 50:
            p.showPage()
            y = 800
    p.save()
    buf.seek(0)
    return StreamingResponse(buf, media_type="application/pdf")


# -----------------------------
# View Feedback History (Manager)
# -----------------------------
@router.get("/manager/{manager_id}", response_model=List[FeedbackOut])
async def get_manager_feedback_history(manager_id: str):
    mgr = await User.find_one(
        User.employee_id == manager_id,
        User.role == "manager"
    )
    if not mgr:
        raise HTTPException(404, "Manager not found")

    fbs = await Feedback.find(
        Feedback.manager_employee_id == manager_id
    ).to_list()

    out = []
    for fb in fbs:
        comments_html = [
            {"employee_id": c["employee_id"], "text": markdown2.markdown(c["text"])}
            for c in getattr(fb, "comments", [])
        ]
        out.append(
            FeedbackOut.from_feedback(
                fb,
                mgr.name,
                comments_html
            )
        )
    return out


# -------------------------------
# Notifications
# -------------------------------
@router.get("/notifications/{employee_id}")
async def get_notifications(employee_id: str):
    notifs = await Notification.find(
        Notification.employee_id == employee_id
    ).sort(-Notification.created_at).to_list()
    return [
        {
            "id": str(n.id),
            "employee_id": n.employee_id,
            "manager_employee_id": n.manager_employee_id,
            "manager_name": n.manager_name,
            "message": n.message,
            "seen": n.seen,
            "created_at": n.created_at,
        }
        for n in notifs
    ]


@router.patch("/notifications/{notification_id}")
async def update_notification_seen(notification_id: str, seen: bool):
    notif = await Notification.get(notification_id)
    if not notif:
        raise HTTPException(404, "Notification not found")
    notif.seen = seen
    await notif.save()
    return {"message": "Notification updated"}


@router.patch("/notifications/mark-all-seen/{employee_id}")
async def mark_all_seen(employee_id: str):
    await Notification.find(
        Notification.employee_id == employee_id
    ).update_many({"$set": {"seen": True}})
    return {"message": "All notifications marked as seen"}
