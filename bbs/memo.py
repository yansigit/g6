from fastapi import APIRouter, Depends, Form, Path, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from common.common import *
from common.database import get_db
from common.models import Member, Memo

router = APIRouter()
templates = MyTemplates(directory=[CAPTCHA_PATH, TEMPLATES_DIR])
templates.env.filters["default_if_none"] = default_if_none
templates.env.globals["captcha_widget"] = captcha_widget


@router.get("/memo")
def memo_list(request: Request, db: Session = Depends(get_db),
                kind: str = Query(default="recv"),
                current_page: int = Query(default=1, alias="page")):
    """
    쪽지 목록
    """
    member = request.state.login_member
    if not member:
        raise AlertCloseException(status_code=403, detail="로그인 후 이용 가능합니다.")

    model = Memo
    join_model = Member
    target_column = model.me_send_mb_id if kind == "recv" else model.me_recv_mb_id
    mb_column = model.me_recv_mb_id if kind == "recv" else model.me_send_mb_id
    query = db.query(model, join_model.mb_id, join_model.mb_nick).outerjoin(join_model, join_model.mb_id==target_column).filter(
        mb_column == member.mb_id,
        model.me_type == kind
    ).order_by(model.me_id.desc())

    # 페이징 처리
    records_per_page = request.state.config.cf_page_rows
    total_records = query.count()
    offset = (current_page - 1) * records_per_page
    memos = query.offset(offset).limit(records_per_page).all()
    
    context = {
        "request": request,
        "kind": kind,
        "memos": memos,
        "total_records": total_records,
        "page": current_page,
        "paging": get_paging(request, current_page, total_records),
    }
    
    return templates.TemplateResponse(f"{request.state.device}/memo/memo_list.html", context)


@router.get("/memo_view/{me_id}")
def memo_view(request: Request, db: Session = Depends(get_db), me_id: int = Path(...)):
    """
    쪽지 상세
    """
    member = request.state.login_member
    if not member:
        raise AlertCloseException(status_code=403, detail="로그인 후 이용 가능합니다.")
    
    # 본인 쪽지 조회
    memo = db.query(Memo).get(me_id)
    if not memo:
        raise AlertException(status_code=404, detail="쪽지가 존재하지 않습니다.", url="/bbs/memo")
    
    kind = memo.me_type
    target_mb_id = memo.me_send_mb_id if kind == "recv" else memo.me_recv_mb_id
    memo_mb_id = memo.me_recv_mb_id if kind == "recv" else memo.me_send_mb_id
    memo_mb_column = Memo.me_recv_mb_id if kind == "recv" else Memo.me_send_mb_id

    if not memo_mb_id == member.mb_id:
        raise AlertException(status_code=403, detail="본인의 쪽지만 조회 가능합니다.", url="/bbs/memo")

    # 상대방 정보 조회
    target = db.query(Member).filter(Member.mb_id==target_mb_id).first()

    # 이전,다음 쪽지 조회
    prev_memo = db.query(Memo).filter(
        Memo.me_id < me_id,
        Memo.me_type == kind,
        memo_mb_column == member.mb_id
    ).order_by(Memo.me_id.desc()).first()
    next_memo = db.query(Memo).filter(
        Memo.me_id > me_id,
        Memo.me_type == kind,
        memo_mb_column == member.mb_id
    ).order_by(Memo.me_id.asc()).first()

    if kind == "recv" and memo.me_read_datetime is None:
        # 받은 쪽지 읽음처리
        now = datetime.now()
        memo.me_read_datetime = now
        send_memo = db.query(Memo).filter(Memo.me_id==memo.me_send_id).first()
        if send_memo:
            send_memo.me_read_datetime = now
        db.commit()

        # 안읽은쪽지 갯수 갱신
        db_member = db.query(Member).filter(Member.mb_id==member.mb_id).first()
        db_member.mb_memo_cnt = get_memo_not_read(member.mb_id)
        db.commit()

    context = {
        "request": request,
        "kind": memo.me_type,
        "memo": memo,
        "target": target,
        "prev_memo": prev_memo,
        "next_memo": next_memo,
    }
    return templates.TemplateResponse(f"{request.state.device}/memo/memo_view.html", context)


@router.get("/memo_form")
def memo_form(request: Request, db: Session = Depends(get_db),
    me_recv_mb_id : str = Query(default=None),
    me_id: int = Query(default=None)
):
    """
    쪽지 작성
    """
    member = request.state.login_member
    if not member:
        raise AlertCloseException(status_code=403, detail="로그인 후 이용 가능합니다.")

    # 쪽지를 전송할 회원 정보 조회
    target = None
    if me_recv_mb_id:
        target = db.query(Member).filter(Member.mb_id==me_recv_mb_id).first()
    
    # 답장할 쪽지의 정보 조회
    memo = db.query(Memo).get(me_id) if me_id else None 

    context = {
        "request": request,
        "target": target,
        "memo": memo,
    }
    return templates.TemplateResponse(f"{request.state.device}/memo/memo_form.html", context)


@router.post("/memo_form_update")
async def memo_form_update(
    request: Request,
    db: Session = Depends(get_db),
    token: str = Form(...),
    recaptcha_response: Optional[str] = Form(alias="g-recaptcha-response", default=""),
    me_recv_mb_id : str = Form(...),
    me_memo: str = Form(...)
):
    """
    쪽지 전송
    """
    config = request.state.config
    member = request.state.login_member
    if not member:
        raise AlertCloseException(status_code=403, detail="로그인 후 이용 가능합니다.")
    
    if not check_token(request, token):
        raise AlertException(f"{token} : 토큰이 유효하지 않습니다. 새로고침후 다시 시도해 주세요.", 403)

    captcha_cls = get_current_captcha_cls(config.cf_captcha)
    if captcha_cls and (not await captcha_cls.verify(config.cf_recaptcha_secret_key, recaptcha_response)):
        raise AlertException("캡차가 올바르지 않습니다.", 400)

    # me_recv_mb_id 공백 제거
    mb_id_list = me_recv_mb_id.replace(" ", "").split(',')
    target_list = []
    error_list = []
    for mb_id in mb_id_list:
        # 쪽지를 전송할 회원 정보 조회
        target = db.query(Member).filter(Member.mb_id==mb_id).first()
        if target and target.mb_open and not(target.mb_leave_date or target.mb_intercept_date):
            target_list.append(target)
        else:
            error_list.append(mb_id)

    if error_list:
        raise AlertException(f"{','.join(error_list)} : 존재(또는 정보공개)하지 않는 회원이거나 탈퇴/차단된 회원입니다.\\n쪽지를 발송하지 않았습니다.", 404)

    # 총 사용 포인트 체크
    use_point = int(config.cf_memo_send_point)
    total_use_point = use_point * len(target_list)
    if total_use_point > 0:
        if member.mb_point < total_use_point:
            raise AlertException(f"보유하신 포인트({member.mb_point})가 부족합니다.\\n쪽지를 발송하지 않았습니다.", 403)

    # 전송대상의 목록을 순회하며 쪽지 전송
    for target in target_list:
        memo_dict = {
            "me_send_mb_id": member.mb_id,
            "me_recv_mb_id": target.mb_id,
            "me_memo": me_memo,
            "me_send_ip": request.client.host,
        }
        memo_send = Memo(me_type='send', **memo_dict)
        db.add(memo_send)
        db.commit()
        memo_recv = Memo(me_type='recv', me_send_id=memo_send.me_id, **memo_dict)
        db.add(memo_recv)
        db.commit()

        # 실시간 쪽지 알림
        target.mb_memo_call = member.mb_id
        target.mb_memo_cnt = get_memo_not_read(target.mb_id)
        db.commit()

        # 포인트 소진
        insert_point(request, member.mb_id, use_point * (-1), f"{target.mb_nick}({target.mb_id})님에게 쪽지 발송", "@memo", target.mb_id, member.mb_id)

    return RedirectResponse(url=f"/bbs/memo?kind=send", status_code=302)


@router.get("/memo_delete/{me_id}")
def memo_delete(request: Request, db: Session = Depends(get_db), 
                me_id: int = Path(...),
                token:str = Query(...),
                page:int = Query(default=1)
                ):
    """
    쪽지 삭제
    """
    if not compare_token(request, token, 'delete'):
        raise AlertException(status_code=403, detail=f"{token} : 토큰이 존재하지 않습니다.")
    
    member = request.state.login_member
    if not member:
        raise AlertCloseException(status_code=403, detail="로그인 후 이용 가능합니다.")
    
    memo = db.query(Memo).get(me_id)
    if not memo:
        raise AlertException(status_code=403, detail="쪽지가 존재하지 않습니다.", url="/bbs/memo")
    
    kind = memo.me_type
    memo_mb_id = memo.me_recv_mb_id if kind == "recv" else memo.me_send_mb_id
    if not memo_mb_id == member.mb_id:
        raise AlertException(status_code=403, detail="본인의 쪽지만 삭제 가능합니다.", url="/bbs/memo")
    
    # 실시간 알림 삭제(업데이트)
    if memo.me_read_datetime is None:
        target_member = db.query(Member).filter(
            Member.mb_id==memo.me_recv_mb_id,
            Member.mb_memo_call==memo.me_send_mb_id
        ).first()
        if target_member:
            target_member.mb_memo_call = ''
            db.commit()

    db.delete(memo)
    db.commit()

    # 안읽은쪽지 갯수 갱신
    db_member = db.query(Member).filter(Member.mb_id==member.mb_id).first()
    db_member.mb_memo_cnt = get_memo_not_read(member.mb_id)
    db.commit()

    return RedirectResponse(url=f"/bbs/memo?kind={kind}&page={page}", status_code=302)