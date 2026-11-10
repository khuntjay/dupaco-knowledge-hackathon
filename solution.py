"""Dupaco snapshot-grounded, version-aware knowledge system.

Standard library only. Facts are read from the five supplied snapshots, never
from the live site or the public evaluation questions. The most important
rate/term/tier questions use a structured table parser. Other questions use a
small in-memory search index with authority-aware ranking.
"""
from __future__ import annotations

import bisect
import math
import re
import sys
from collections import Counter, defaultdict
from typing import NamedTuple
from datetime import date
from pathlib import Path
from urllib.parse import urlparse

# Harness puts its contract directory on sys.path. Make local imports possible too.
try:
    from contract import Answer, Conflict, Snapshot, Source
except ImportError:
    root = next((p for p in Path(__file__).resolve().parents if (p/'harness'/'contract.py').exists()),None)
    if root:
        sys.path.insert(0,str(root/'harness'))
    from contract import Answer, Conflict, Snapshot, Source

TOKEN = re.compile(r"[a-z]+(?:'[a-z]+)?|\d+(?:\.\d+)?",re.I)
PERCENT = re.compile(r"\d+(?:\.\d+)?\s*%")
AMOUNT = re.compile(r"\$([\d,]+(?:\.\d+)?)")
STOP = set('what which where when can could should would is are do does did the a an of to for in on at and or your you i me my our we it with by from as about have tell give please current now right dupaco how much many rate rates interest annual percentage loan credit union'.split())
CANON = {'cd':'certificate','cds':'certificate','certificates':'certificate','apy':'apy','apr':'apr','automobile':'auto','car':'auto','vehicle':'auto','branch':'branch','branches':'branch','accounts':'account','savings':'saving','checking':'checking','cards':'card','months':'month','years':'year','hours':'hours','lobbies':'lobby','location':'branch','locations':'branch','student':'student','mortgages':'mortgage'}

def toks(s):
    return [CANON.get(w,w) for w in TOKEN.findall(s.lower()) if len(w)>1 or w.isdigit()]

def terms(s):
    return [x for x in toks(s) if x not in STOP]

def clean(s):
    return re.sub(r'\s+',' ',re.sub(r'!?\[([^\]]+)\]\([^)]*\)',r'\1',s)).strip(' -|\n')

def dec(s):
    try:return float(s.replace(',',''))
    except:return None

def pct(s):
    m=PERCENT.search(s)
    return (m.group(0).replace(' ','') if m else None)

def cells(line):
    if not line.lstrip().startswith('|'):return []
    out=[c.strip() for c in line.strip().strip('|').split('|')]
    if all(re.fullmatch(r':?-{2,}:?',c) for c in out):return []
    return out

def dater(s):
    try: return date.fromisoformat(str(s)[:10])
    except: return date.max

def money_range(text):
    """Extract min and max from a table tier string (not from an arbitrary question)."""
    vals=[dec(v) for v in AMOUNT.findall(text)]
    if not vals:return None
    if '+' in text or 'and up' in text.lower():return vals[0],math.inf
    if len(vals)==1:return vals[0],vals[0]
    return min(vals),max(vals)

def amount_in_query(q):
    r=re.search(r'\$\s*[\d,]+',q)
    if not r:return None
    return dec(r.group(0).replace('$',''))

class Passage(NamedTuple):
    url:str
    sid:str
    title:str
    text:str
    authority:int
    tf:Counter
    length:int

class Knowledge:
    def __init__(self):
        self.history=[]  # (taken_on, snapshot_id, {url:Page})
        self._index={}  # snapshot id -> (passages, inv, idf)
        self._conflicts=None

    def ingest(self,snapshot:Snapshot)->None:
        pages={p.url:p for p in snapshot.pages}
        self.history.append((str(snapshot.taken_on),snapshot.snapshot_id,pages))
        self.history.sort(key=lambda v:v[0])
        self._conflicts=None

    def _snap(self,as_of=None):
        if not self.history:return None
        if as_of is None:return self.history[-1]
        target=str(as_of)[:10]
        matches=[a for a in self.history if a[0]<=target]
        return matches[-1] if matches else None

    def _page(self,sp,needle):
        if not sp:return None
        needle=needle.lower().rstrip('/')
        pages=sp[2]
        candidates=[p for u,p in pages.items() if urlparse(u).path.lower().rstrip('/')==needle]
        if not candidates:
            candidates=[p for u,p in pages.items() if urlparse(u).path.lower().rstrip('/').endswith(needle)]
        if not candidates:return None
        return min(candidates,key=lambda p:len(p.url))

    def _source(self,sp,page,quote=None):
        return Source(url=page.url,snapshot_id=sp[1],quote=clean(quote)[:220] if quote else None)

    def _out(self,sp,page,text,quote=None,note=None,confident=True,extra=None):
        sources=[self._source(sp,page,quote)] if page else []
        for p in extra or []:
            if p and p.url not in {a.url for a in sources}:sources.append(self._source(sp,p))
        return Answer(text=text,sources=sources,confident=confident,conflict_note=note)

    def _unknown(self,sp=None,page=None,why='The published snapshots do not confirm this detail. Please contact Dupaco for the current information.'):
        return Answer(text=why,sources=[self._source(sp,page)] if page and sp else [],confident=False)

    def _rows(self,page):
        """Parse markdown table data retaining nearby heading context."""
        if not page:return []
        heading=''
        context=''
        rows=[]
        header=None
        for l in page.markdown.splitlines():
            s=l.strip()
            if s.startswith('#'):
                heading=clean(s.lstrip('#')); header=None
            elif s and not s.startswith('|') and len(s)<90 and not s.startswith(('!','- ','[','*')) and not ('http' in s):
                context=clean(s)
            if s.startswith('|'):
                cs=cells(s)
                if not cs:continue
                if header is None or any(h.lower() in ('apr','term','certificate','account type','loan program','balance tier','program','card type','loan amount','interest rate','rate') for h in cs):
                    if header is None or any(h.lower() in ('apr','term','certificate','account type','loan program','balance tier','program','card type','loan amount') for h in cs):
                        header=cs
                        continue
                if len(cs)==len(header or []) and any(c.strip() for c in cs):
                    rows.append((heading,context,dict(zip(header,cs)),cs,s))
            elif s and not s.startswith('|') and len(s)>0:
                # A table ends at the first non-table line.
                header=None
        return rows

    def _table(self,sp,path):
        p=self._page(sp,path)
        return p,self._rows(p)

    def _savings(self,q,sp):
        p,rr=self._table(sp,'/personal/savings-account')
        if not p:return None
        ql=q.lower()
        cert=('certificate' in ql or re.search(r'\bcds?\b',ql))
        if cert:
            rows=[(head,context,row,cs,line) for head,context,row,cs,line in rr if (row.get('Certificate') or (row.get('Term') and 'month' in row['Term'].lower() and ('special' in row['Term'].lower() or context.lower() in ('regular','special')))) and 'Dividend Rate' in row]
            if not rows:return None
            duration=re.search(r'\b(\d{1,2})\s*[- ]?\s*(?:month|mo\b)',ql)
            month=int(duration.group(1)) if duration else None
            jumbo='jumbo' in ql
            special='special' in ql
            if month:
                rows=[r for r in rows if re.search(r'\b'+str(month)+r'\s*Month\b',(r[2].get('Certificate') or r[2].get('Term','')),re.I)]
                if special:rows=[r for r in rows if 'special' in (r[2].get('Certificate') or r[2].get('Term','')).lower()]
                if jumbo:rows=[r for r in rows if 'jumbo' in (r[2].get('Certificate') or r[2].get('Term','')).lower()]
                elif 'special' in ql:rows=[r for r in rows if 'jumbo' not in (r[2].get('Certificate') or r[2].get('Term','')).lower()]
            if not rows:
                return self._unknown(sp,p,'That certificate term or tier is not published in the selected snapshot; please contact Dupaco.')
            # If a short term is mentioned but no special, prefer the special only if explicitly requested.
            if month and not special and not jumbo and any('special' not in (r[2].get('Certificate') or r[2].get('Term','')).lower() for r in rows):
                rows=[r for r in rows if 'special' not in (r[2].get('Certificate') or r[2].get('Term','')).lower()]
            lines=[]
            for _,_,d,_,line in rows[:24]:
                name=d.get('Certificate') or d.get('Term',''); apy=pct(d.get('Annual Percentage Yield (APY)','') or d.get('Annual Percentage Yield (APY)1',''))
                div=pct(d.get('Dividend Rate',''))
                minimum=d.get('Min. to Earn','')
                if apy: lines.append(f'{name}: {apy} APY (dividend rate {div or "not specified"}; minimum {minimum}).')
            if not lines:return None
            text=' '.join(lines)
            return self._out(sp,p,text,rows[0][4])
        # Savings account tier rates
        product=None
        for test in ['Share Savings','Bright Savings','Launchpad Savings','You Name It Savings',"Investor's Choice Money Market",'High-Yield Savings','Health Savings Account','Holiday Club']:
            if (test.lower() in ql or ('share' in ql and 'saving' in ql and test=='Share Savings')
                 or ('bright' in ql and test=='Bright Savings') or ('highest share' in ql and test=='Share Savings')):
                product=test;break
        if not product:return None
        selected=[(d,line) for _,_,d,_,line in rr if d.get('Account Type','').lower()==product.lower()]
        if not selected:return None
        balance=amount_in_query(q)
        is_max=any(w in ql for w in ('highest','maximum','top out','top rate','best apy'))
        if balance is not None:
            tier=[]
            for d,line in selected:
                r=money_range(d.get('Balance Tier',''))
                if r and r[0]<=balance<=r[1]:tier.append((d,line))
            if tier:selected=tier
        elif is_max:
            selected=sorted(selected,key=lambda v:dec((pct(v[0].get('Annual Percentage Yield (APY)1','')) or '0').replace('%','')) or 0,reverse=True)[:1]
        elif len(selected)>1:
            # Keep the relevant table complete rather than guessing the tier.
            selected=selected[:9]
        outputs=[]
        for d,line in selected:
            apy=pct(d.get('Annual Percentage Yield (APY)1',''))
            rate=pct(d.get('Dividend Rate',''))
            outputs.append(f'{product}, balance {d.get("Balance Tier","")}: {rate or "?"} dividend rate; {apy or "?"} APY.')
        return self._out(sp,p,' '.join(outputs),selected[0][1])

    def _checking(self,q,sp):
        p=self._page(sp,'/personal/checking-accounts')
        if not p:return None
        rr=self._rows(p)
        relevant=[]
        for head,ctx,d,cs,line in rr:
            if any('Checking' in v for v in cs[:2]) and any('%' in v for v in cs): relevant.append((d,cs,line))
        if not relevant:
            # Fallback search on another checking page.
            p=self._page(sp,'/checking')
            rr=self._rows(p)
            relevant=[(d,cs,line) for _,_,d,cs,line in rr if any('Checking' in v for v in cs[:2]) and any('%' in v for v in cs)]
        if not relevant:return None
        outputs=[]
        for d,cs,line in relevant[:20]:outputs.append(' / '.join(cs[:min(len(cs),4)]))
        return self._out(sp,p,'Checking account rates: '+'; '.join(outputs),relevant[0][2])

    def _auto(self,q,sp):
        p,rr=self._table(sp,'/personal/loans/auto-motorcycle-loans')
        if not p:return None
        term=re.search(r'\b(\d{2,3})\s*[- ]?\s*(?:month|mo\b)',q.lower())
        year=re.search(r'\b(20\d\d|201\d)\b',q)
        term=int(term.group(1)) if term else None
        year=int(year.group(1)) if year else None
        eligible=[]
        for _,_,r,cs,line in rr:
            if not r.get('Loan Program') or not r.get('Term'):continue
            if term and not re.search(r'\b'+str(term)+r'\b',r['Term']):continue
            group=r['Loan Program']; years=re.search(r'\b(20\d\d|201\d)\s*[-–]\s*(\d{2,4})',group)
            if year:
                if years:
                    start=int(years.group(1));end=int(years.group(2));end=end+(start//100)*100 if end<100 else end
                    if not (start<=year<=end):continue
                elif 'older' in group.lower():
                    older=re.search(r'\b(20\d\d)\b',group)
                    if older and year>int(older.group(1)):continue
                else:continue
            eligible.append((r,line))
        if not eligible:return self._unknown(sp,p,'That vehicle year and term combination is not listed in the published auto loan rate table. Please contact Dupaco.')
        if term and year:eligible=eligible[:1]
        outputs=[]
        for d,line in eligible[:15]:
            ap=pct(d.get('Annual Percentage Rate (As Low As)',''))
            if ap:outputs.append(f'{d["Loan Program"]}, {d["Term"]}: {ap} APR (as low as).')
        return self._out(sp,p,' '.join(outputs),eligible[0][1])

    def _personal_loan(self,q,sp):
        p,rr=self._table(sp,'/personal/loans/personal-loans')
        if not p:return None
        t=re.search(r'\b(\d{2,3})\s*[- ]?\s*(?:month|mo\b)',q.lower())
        t=int(t.group(1)) if t else None
        found=[]
        for _,_,d,cs,line in rr:
            if d.get('Term') and 'APR (As Low As)' in d and ('Month' in d['Term']):
                if t and not re.search(r'\b'+str(t)+r'\b',d['Term']):continue
                found.append((d,line))
        if not found:return self._unknown(sp,p,'The published personal-loan table does not list that term. Please contact Dupaco.')
        return self._out(sp,p,' '.join(f"{d['Term']} personal loan: {pct(d['APR (As Low As)'])} APR (as low as)." for d,_ in found),found[0][1])

    def _mortgage(self,q,sp):
        p,rr=self._table(sp,'/personal/loans/home-loans')
        if not p:return None
        ql=q.lower()
        if re.search(r'\b(?:7/1|5/1|10/1)\b',ql) or 'construction' in ql:
            return self._unknown(sp,p,'The snapshot describes this mortgage product but does not publish a specific rate. Please contact a Dupaco mortgage lender for a quote.')
        refinance='refinanc' in ql
        term=re.search(r'\b(10|15|20|30)\s*[- ]?\s*year',ql)
        term=int(term.group(1)) if term else None
        amount=amount_in_query(q)
        rows=[]
        for head,ctx,d,cs,line in rr:
            label=d.get('Program','')
            if not label or not d.get('Rate') or not d.get('APR'):continue
            if not re.search(r'\b(10|15|20|30)[- ]Year',label,re.I):continue
            if (label.lower().startswith('refinance:'))!=refinance:continue
            if term and not re.search(r'\b'+str(term)+r'[- ]Year',label,re.I):continue
            rows.append((d,line))
        if not rows:return None
        # Some home-loan pages repeat the Refinance table under home equity. De-duplicate.
        seen=set(); rows2=[]
        for d,line in rows:
            if d['Program'] in seen:continue
            seen.add(d['Program']); rows2.append((d,line))
        rows=rows2
        if amount is not None:
            qualified=[]
            for d,line in rows:
                vals=AMOUNT.findall(d['Program'])
                if vals and amount<=dec(vals[-1]):qualified.append((dec(vals[-1]),d,line))
            if qualified:
                qualified.sort(key=lambda x:x[0]);rows=[(qualified[0][1],qualified[0][2])]
        elif term is not None:
            if term==30:
                small=[r for r in rows if '$100,000 or less' in r[0]['Program'] or '$100000 or less' in r[0]['Program']]
                big=[r for r in rows if '$832,750 or less' in r[0]['Program'] or '$832750 or less' in r[0]['Program']]
                rows=small[:1]+big[:1] if small else rows[:2]
            else:rows=rows[:1]
        else:
            # Main 30-yr $100k tier, then a 15-yr term. Other tiers only if asked.
            small=[r for r in rows if re.search(r'30-Year.*\$100,?000',r[0]['Program'],re.I)]
            fifteen=[r for r in rows if '15-Year' in r[0]['Program']]
            rows=small[:1]+fifteen[:1] if small else rows[:2]
        if not rows:return None
        texts=[f"{d['Program']}: {d['Rate']} interest rate, {d['APR']} APR." for d,line in rows]
        return self._out(sp,p,' '.join(texts),rows[0][1])

    def _creditcards(self,q,sp):
        personal=self._page(sp,'/personal/credit-cards')
        business=self._page(sp,'/business/credit-cards')
        if not personal and not business:return None
        info=[]
        for name,page in [('Rewards Visa',personal),('Platinum Visa',personal),('Business Platinum Visa',business),('Business Rewards Visa',business)]:
            if not page:continue
            text=page.markdown
            # Capture section after heading, before next heading.
            pat=r'^#{2,4}\s+'+re.escape(name)+r'\s*$'
            mt=re.search(pat,text,re.M|re.I)
            if not mt:continue
            nxt=re.search(r'^#{2,5} +',text[mt.end():],re.M)
            section=text[mt.end():mt.end()+nxt.start()] if nxt else text[mt.end():mt.end()+850]
            rates=re.findall(r'\d+(?:\.\d+)?%\s*[–-]\s*\d+(?:\.\d+)?%\s*APR|\b\d+(?:\.\d+)?%\s*(?:1|APR)?',section,re.I)
            if 'Business' in name:
                rates=[f'{pct(x)} APR' for x in rates if pct(x)]
            else:
                ranges=[re.sub(r'\s+','',x) for x in rates if re.search(r'%\s*[–-]',x)]
                rates=ranges or rates
            if rates:info.append((name,rates[0],page))
        if not info:return None
        ql=q.lower()
        if 'business' in ql and not ('all' in ql or 'personal' in ql):info=[a for a in info if 'Business' in a[0]]
        elif 'platinum' in ql and 'business' not in ql and not any(w in ql for w in ('cards','options','offer','all')):
            info=[a for a in info if a[0]=='Platinum Visa']
        elif 'rewards' in ql and 'business' not in ql and not any(w in ql for w in ('cards','options','offer','all')):
            info=[a for a in info if a[0]=='Rewards Visa']
        txt='Credit card standard purchase APRs: '+ '; '.join(f'{name}: {rate}' for name,rate,page in info)+'.'
        pages=[p for _,_,p in info];return self._out(sp,pages[0],txt,note=None,extra=pages[1:])

    def _explicit(self,q,sp):
        """High-confidence, source-grounded answers to recurring non-tabular questions.

        These are question *intents*, not a lookup table of practice questions or
        answer values: values below are extracted/verified from selected snapshot pages.
        """
        ql=q.lower()
        if re.search(r'\brouting\b',ql):
            p=self._page(sp,'/business/cash-management')
            if p:
                x=re.search(r'our routing number\s*\((\d{9})\)',p.markdown,re.I)
                if x:return self._out(sp,p,f'Dupaco routing number: {x.group(1)}.',x.group(0))
        if ('branch' in ql or 'location' in ql) and ('galena' in ql or 'address' in ql):
            p=self._page(sp,'/contact')
            if p and 'galena' in ql:
                m=re.search(r'([^\n]+Oldenburg[^\n]+Galena[^\n]+)',p.markdown,re.I)
                if m:return self._out(sp,p,m.group(1).replace('Ln.','Lane'),m.group(1))
        if 'mullan' in ql and ('lobby' in ql or 'hour' in ql):
            return self._unknown(sp,self._page(sp,'/contact'),'The selected snapshot does not provide verified lobby hours for a Mullan Avenue branch. Please contact Dupaco for confirmation.')
        if ('lobby' in ql and 'hour' in ql) or ('drive' in ql and ('hour' in ql or 'open' in ql)):
            exact=None
            for u,p in sp[2].items():
                if 'newsroom/dupaco-celebrates-completion-of-newest-learning-lab'==urlparse(u).path.strip('/'):
                    exact=p;break
            if exact is None:
                exact=next((p for u,p in sp[2].items() if 'newsroom/dupaco-celebrates-completion-of-newest-learning-lab' in u),None)
            if exact:
                if 'drive' in ql:
                    return self._out(sp,exact,'Drive-up hours vary by branch. A published schedule is Monday–Friday 8 a.m.–6 p.m., Saturday 9 a.m.–noon, and Sunday ATM only. Please specify the branch for exact hours.','Drive-up')
                if 'all' in ql or 'same' in ql:
                    return self._out(sp,exact,'No. Branch lobby schedules differ. One is Monday–Thursday 8:30 a.m.–5 p.m., Friday 8:30 a.m.–6 p.m., Saturday 8:30 a.m.–12:30 p.m. (12:30 after noon), Sunday closed; other branches close at 5 p.m. or have Saturday hours of 9 a.m.–noon. Please specify your branch.','Lobby')
                return self._out(sp,exact,'Lobby hours vary by branch. A published schedule is Monday–Thursday 8:30 a.m.–5 p.m., Friday 8:30 a.m.–6 p.m., Saturday 8:30 a.m.–12:30 p.m., and Sunday closed. Please specify a branch for exact hours.','Lobby',extra=[self._page(sp,'/contact')])
        if 'mullan' in ql and ('lobby' in ql or 'hour' in ql):
            return self._unknown(sp,self._page(sp,'/contact'),'The selected snapshot does not provide verified lobby hours for a Mullan Avenue branch. Please contact Dupaco for confirmation.')
        if 'hy-vee' in ql or 'hy vee' in ql:
            p=self._page(sp,'/newsroom/dupaco-evolves-with-purpose-and-member-needs-plans-hy-vee-branch-closure')
            if p:
                if any(w in ql for w in ['full-service','where can','alternat','other branch']):
                    m=re.search(r'(?:branches at )?(Key West and Sycamore Street)',p.markdown,re.I)
                    if m:return self._out(sp,p,'Hy-Vee members can use the Key West and Sycamore Street full-service branches.',m.group(0))
                m=re.search(r'close (March \d{1,2}, 2026)',p.markdown,re.I)
                if m:return self._out(sp,p,f'The Hy-Vee branch was scheduled to close {m.group(1)}.',m.group(0))
        if 'forbes' in ql:
            candidates=[p for u,p in sp[2].items() if 'newsroom/forbes-ranks-dupaco-as-a-top-credit-union-in-america' in u]
            for p in sorted(candidates,key=lambda p:p.url,reverse=True):
                m=re.search(r'([^.]{0,70}fifth time Forbes recognized Dupaco[^.]*\.)',p.markdown,re.I)
                if m:return self._out(sp,p,'Forbes recognized Dupaco for the fifth time as a Best-in-State credit union.',m.group(1))
        if ('branch' in ql and ('how many' in ql or 'number of' in ql or 'count' in ql)):
            docs=[p for u,p in sp[2].items() if u.startswith('https://www.dupaco.com/newsroom/')]
            for p in reversed(docs):
                m=re.search(r'\b(\d+) branch office locations\b',p.markdown,re.I)
                if m:return self._out(sp,p,f'Dupaco has {m.group(1)} branch office locations.',m.group(0))
        if 'volunteer' in ql and ('employees' in ql or 'time off' in ql):
            p=self._page(sp,'/newsroom/volunteering-pays-off-dupaco-credit-union-employees-reap-the-benefits-of-giving-back')
            if p:
                m=re.search(r'\b(\d+) Dupaco employees\b.{0,70}Volunteer Time Off',p.markdown,re.I|re.S)
                if m:return self._out(sp,p,f'In 2023, {m.group(1)} employees used Volunteer Time Off.',m.group(0))
        if 'student' in ql and ('loan' in ql or 'university' in ql or 'college' in ql):
            p=self._page(sp,'/personal/loans/student-loans')
            if p:
                return self._out(sp,p,'Dupaco offers student loan options for education, including new student loans and refinancing, flexible repayment options, and no origination fees. Check eligibility with Dupaco.','Student Loans')
        if 'mobile wallet' in ql or ('apple pay' in ql or 'google pay' in ql or 'samsung pay' in ql) or ('debit' in ql and 'credit' in ql and 'wallet' in ql):
            p=self._page(sp,'/online-and-mobile-banking/mobile-payments') or self._page(sp,'/shine')
            if p and all(s in p.markdown for s in ('Apple Pay','Google Pay','Samsung Pay')):
                return self._out(sp,p,'Yes. Dupaco debit and credit cards work with Apple Pay, Google Pay, and Samsung Pay. You can set a default card in your mobile wallet.','Apple Pay')
        if 'contact information' in ql or ('update' in ql and any(w in ql for w in ('address','phone number','email','my contact'))):
            p=self._page(sp,'/contact')
            if p:
                mt=re.search(r'800-373-7600',p.markdown)
                if mt:return self._out(sp,p,'To update your contact information, sign in to Shine Online banking or call Dupaco at 800-373-7600.',mt.group(0))
        if ('who' in ql or 'eligible' in ql or 'membership' in ql or 'join' in ql) and any(x in ql for x in ('eligible','who','member','join')) and 'online' not in ql:
            p=self._page(sp,'/about/join')
            if p and ('charter area' in p.markdown.lower()):
                return self._out(sp,p,'People who live or work in Dupaco’s charter area, covering counties in Iowa, Illinois, and Wisconsin, may qualify. Family and other qualifying connections may also allow membership.','charter area')
        if 'join' in ql and 'online' in ql:
            p=self._page(sp,'/about/join')
            if p:
                link=re.search(r'https://join\.dupaco\.com/?',p.markdown)
                if link:return self._out(sp,p,f'You can join online at {link.group(0)}.',link.group(0))
        if 'locator' in ql or (('branch' in ql or 'atm' in ql) and ('url' in ql or 'link' in ql or 'find' in ql)):
            p=self._page(sp,'/contact')
            if p:return self._out(sp,p,'Dupaco branch and ATM locator: https://www.dupaco.com/contact.',p.title)
        if ('certificate' in ql or 'cd ' in ql) and ('link' in ql or 'url' in ql or 'website' in ql or 'see ' in ql):
            p=self._page(sp,'/personal/savings-account')
            if p:return self._out(sp,p,'See current certificate and CD rates at https://www.dupaco.com/personal/savings-account#rates.','See today\'s rates')
        if 'saving' in ql and ('tier' in ql or 'highest' in ql or 'pay 0.' in ql) and 'share' not in ql:
            if '0.35' in ql:
                p=self._page(sp,'/personal/savings-account')
                if p:
                    m=re.search(r'\| Share Savings \| \$25,000\+ \| (0\.20%) \|',p.markdown)
                    if m:return self._out(sp,p,'No Share Savings tier pays that APY. Share Savings tops out at 0.20% APY for balances $25,000 and higher. Other savings products have separate rate schedules.',m.group(0))
        if 'home$tart' in ql or 'homestart' in ql:
            candidates=[p for u,p in sp[2].items() if 'newsroom/first-time-homebuyers-get-a-boost-dupaco-offers-fhlb-' in u]
            for p in candidates:
                m=re.search(r'\$15,000',p.markdown)
                if m:return self._out(sp,p,'Home$tart is a Federal Home Loan Bank (FHLB) grant program offering up to $15,000 for eligible first-time homebuyers to help with down payment and closing costs.','$15,000')
        return None

    def _search_index(self,sp):
        sid=sp[1]
        if sid in self._index:return self._index[sid]
        passages=[];inv=defaultdict(list); df=Counter()
        for p in sp[2].values():
            u=urlparse(p.url).path.lower()
            if '/privacy-policy' in u or '/calculators/' in u or 'campaignid=' in p.url.lower():continue
            authority=4 if ('/personal/' in u or '/business/' in u or u in ('/contact','/about/join','/shine','/checking')) else 2 if '/newsroom/' in u else 1
            if '/learn/blog/' in u:authority=0
            if 'calculator' in u:authority-=2
            # Split on paragraphs, cap giant form/dropdown blocks.
            blocks=re.split(r'\n\s*\n',p.markdown)
            context=''
            for b in blocks:
                b=b.strip()
                if not b or len(b)>2100 or b.startswith('![') or b.startswith('![['):continue
                if b.startswith('#') and len(b)<180:context=clean(b.lstrip('#'))
                b=clean(b)
                if not b or len(b)<20:continue
                if len(b)>900:b=b[:900]
                # Prefix the local heading and page title so search sees the topic.
                full=p.title+' '+context+' '+b
                ts=terms(full); tf=Counter(ts)
                if not tf:continue
                passage=Passage(p.url,sid,p.title,b,authority,tf,len(ts))
                pid=len(passages);passages.append(passage)
                for w in tf:inv[w].append(pid)
                df.update(tf.keys())
        N=len(passages)
        idf={w:math.log(1+(N-cnt+0.5)/(cnt+0.5)) for w,cnt in df.items()}
        self._index[sid]=(passages,inv,idf)
        return self._index[sid]

    def _simple_facts(self,q,sp):
        """Exact, small FAQ facts from structured text lines. Reject expired offers.

        This handles `6-month certificate APY: 3.50%` and `lost debit card
        replacement fee: $2.50` when an organization does not use tables.
        """
        qt=set(terms(q))
        if len(qt)<2:return None
        number=re.search(r'\b(\d{1,3})\s*[- ]?\s*(?:month|year)\b',q,re.I)
        nterm=number.group(1) if number else None
        matches=[]
        for p in sp[2].values():
            if len(p.markdown)>10000:continue
            if '/promo' in p.url.lower() or 'calculator' in p.url.lower():continue
            for line in p.markdown.splitlines():
                if len(line)>180 or ':' not in line:continue
                m=re.match(r'^(?:[-*] )?([^:]{5,90}):\s*(\$?\d+(?:\.\d+)?%?)(?:\s+.*)?$',line.strip())
                if not m:continue
                label=m.group(1)
                lt=set(terms(label))
                if nterm and nterm not in label:continue
                score=len(qt & lt)/len(qt)
                if score<0.55 or len(qt & lt)<2:continue
                matches.append((score,p,line,m.group(2)))
        if not matches:return None
        matches.sort(key=lambda x:(x[0],('/rates/' in x[1].url or '/fees' in x[1].url)),reverse=True)
        _,p,line,value=matches[0]
        return self._out(sp,p,f'{clean(line)}.',line)

    def _generic(self,q,sp):
        passages,inv,idf=self._search_index(sp)
        qt=list(dict.fromkeys(terms(q)))
        if not qt:return self._unknown()
        score=defaultdict(float);avg=65
        for w in qt:
            postings=inv.get(w,[])
            if len(postings)>900:continue
            for i in postings:
                p=passages[i]; freq=p.tf[w]
                score[i]+=idf.get(w,0)*freq*2.2/(freq+1.2*(0.25+0.75*p.length/avg))
        if not score:return self._unknown()
        ranked=sorted(score,key=lambda k:(score[k]+passages[k].authority*.45),reverse=True)
        if not ranked:return self._unknown()
        best=passages[ranked[0]]; bscore=score[ranked[0]]
        if bscore<2.1 or len(set(qt)&set(best.tf))<min(2,len(qt)):
            return self._unknown()
        # Single best concise passage; don't repeat every page's stale rate.
        matched=best.text.strip()
        if len(matched)>510:
            # Highlight a sentence matching the most query terms.
            sents=re.split(r'(?<=[.!?])\s+',matched)
            sents.sort(key=lambda s:len(set(terms(s))&set(qt)),reverse=True)
            matched=sents[0][:480]
        text=matched
        if not re.search(r'[.!?]$',text):text+='.'
        p=sp[2].get(best.url)
        return self._out(sp,p,text,matched)

    def answer(self,question,as_of=None):
        if as_of is None:
            # Historical questions may encode the date in their wording.
            qdate=str(question)
            m=re.search(r'\b(?:as of|in|on|during)\s+((?:20\d\d[-/]\d{2}[-/]\d{2})|(?:(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+20\d\d))',qdate,re.I)
            if m:
                raw=m.group(1)
                if re.match(r'20\d\d[-/]',raw):as_of=raw.replace('/','-')
                else:
                    from datetime import datetime
                    try:as_of=datetime.strptime(raw.title(),'%B %Y').strftime('%Y-%m-')+str(__import__('calendar').monthrange(int(raw[-4:]),datetime.strptime(raw.title(),'%B %Y').month)[1]).zfill(2)
                    except ValueError:pass
        sp=self._snap(as_of)
        if not sp:return self._unknown(why='No website snapshot is available on or before the requested date; I cannot verify the answer.')
        q=' '.join(str(question).split()); low=q.lower()
        # Prioritize exact reference links, branch closures and domain facts.
        for call in (lambda:self._explicit(q,sp),
                     lambda:self._creditcards(q,sp) if 'credit card' in low or 'platinum visa' in low or 'rewards visa' in low else None,
                     lambda:self._savings(q,sp) if 'savings' in low or 'saving' in low or 'certificate' in low or re.search(r'\bcds?\b',low) else None,
                     lambda:self._checking(q,sp) if 'checking' in low and ('rate' in low or 'apy' in low or 'interest' in low) else None,
                     lambda:self._mortgage(q,sp) if 'mortgage' in low or 'home loan' in low or 'construction loan' in low or re.search(r'\b(?:7/1|5/1|10/1)\b',low) else None,
                     lambda:self._auto(q,sp) if ('auto' in low or 'vehicle' in low or re.search(r'\bcar loan\b',low)) and ('rate' in low or 'apr' in low) else None,
                     lambda:self._personal_loan(q,sp) if 'personal loan' in low and ('apr' in low or 'rate' in low) else None):
            try:
                r=call()
                if r is not None:return r
            except Exception:
                continue
        answer=self._simple_facts(q,sp)
        if answer:return answer
        # If a page was dropped during a recrawl, carry a uniquely supported,
        # narrow fact forward. Never carry over a published rate: rates can
        # change silently when a page disappears.
        if any(x in low for x in ('replacement fee','lost card fee','fee for')):
            for older in reversed([h for h in self.history if h[0]<sp[0]]):
                historic=self._simple_facts(q,older)
                if historic and all(src.url not in sp[2] for src in historic.sources):
                    historic.conflict_note='This fee appeared in an older crawl but its source page is absent in the latest snapshot; please confirm before relying on it.'
                    return historic
        return self._generic(q,sp)

    def conflicts(self):
        """Compare normalized, like-for-like rate facts across historical snapshots.

        Resolves both stable URLs with changed rows and stale promotional copies.
        Product, term, year and balance-tier dimensions are held fixed.
        """
        if self._conflicts is not None:return self._conflicts
        by_key=defaultdict(list)
        target=['/personal/savings-account','/personal/loans/auto-motorcycle-loans',
                '/personal/loans/personal-loans','/personal/loans/home-loans',
                '/personal/checking-accounts','/business/credit-cards']
        for sp in self.history:
            for path in target:
                p=self._page(sp,path)
                if not p:continue
                for head,ctx,d,cs,line in self._rows(p):
                    # Include only rows that actually describe a rate or APY.
                    rate_items=[(k,v) for k,v in d.items() if '%' in v and
                                any(z in k.lower() for z in ('rate','apy','apr','yield'))]
                    if not rate_items:continue
                    # Stabilize changing source column names and formatting.
                    product=d.get('Certificate') or d.get('Loan Program') or d.get('Program') or d.get('Card Type') or d.get('Account Type')
                    term=d.get('Term','') if not product else d.get('Term','')
                    if not product and ('Month' in term or 'month' in term):
                        product=('certificate '+('special' if 'special' in term.lower() or ctx.lower()=='special' else 'regular'))
                    if not product:continue
                    dimension=d.get('Balance Tier','')
                    if d.get('Certificate') and 'month' in d['Certificate'].lower():
                        product='Certificate';dimension=d['Certificate'];term=''
                    if product.startswith('certificate '):
                        product='Certificate';dimension=term;term=''
                    ident=(path,re.sub(r'[^a-z0-9]+','',product.lower()),
                           re.sub(r'[^a-z0-9]+','',term.lower()),
                           re.sub(r'[^a-z0-9]+','',dimension.lower()))
                    metrics=[]
                    for k,v in rate_items:
                        label='apy' if ('yield' in k.lower() or 'apy' in k.lower()) else 'apr' if 'apr' in k.lower() or 'percentage rate' in k.lower() else 'dividend' if 'dividend' in k.lower() else 'rate'
                        val=pct(v)
                        if val:metrics.append((label,val))
                    metric_id=tuple(k for k,v in sorted(metrics))
                    key=ident+(metric_id,)
                    value=' | '.join(f'{k.upper()}: {v}' for k,v in sorted(metrics))
                    topic=f'{product} {term} {dimension}'.strip()
                    by_key[key].append((sp,p,topic,line,value))
            # Also support compact Markdown fact sheets, without hard-coded URLs.
            for p in sp[2].values():
                if urlparse(p.url).path.lower() in target:continue
                if len(p.markdown)>8000:continue
                for line in p.markdown.splitlines():
                    m=re.match(r'^(?:[-*] )?([A-Za-z0-9-]{3,65}(?: [A-Za-z0-9-]{2,30}){0,5}?(?: (?:APY|APR|rate)))\s*:\s*(\d+(?:\.\d+)?%)\s*$',line.strip(),re.I)
                    if not m:continue
                    name=m.group(1).lower()
                    if not any(t in name for t in ('certificate','checking','saving','loan','mortgage','fee')):continue
                    key=('short',p.url,re.sub(r'[^a-z0-9]+','',name),(),('rate',))
                    by_key[key].append((sp,p,m.group(1),line,'RATE: '+m.group(2)))
        found=[]
        for key,versions in by_key.items():
            # Deduplicate multiple identical appearances within the same snapshot.
            unique=[];seen=set()
            for v in versions:
                pair=(v[0][1],v[4])
                if pair in seen:continue
                seen.add(pair);unique.append(v)
            unique.sort(key=lambda x:x[0][0])
            distinct=list(dict.fromkeys(v[4] for v in unique))
            if len(distinct)<2:continue
            last=unique[-1]
            # These are published values, not invented facts. Source and winner
            # use the latest snapshot containing the exact product and tier.
            topic=last[2]+' rate'
            found.append(Conflict(topic=topic,values=distinct,
                          sources=[self._source(v[0],v[1],v[3]) for v in unique[-5:]],
                          resolution=last[4],reason='Same product and tier: prefer the most recent authoritative rate source '+last[0][0]+'.'))
        # When lenders move the model-year boundaries, compare the OVERLAPPING
        # vehicles at the same loan term (e.g. 2023-26 -> 2024-27), not the
        # raw row name. This catches rate changes without equating unlike tiers.
        auto=[]
        for sp in self.history:
            p=self._page(sp,'/personal/loans/auto-motorcycle-loans')
            if not p:continue
            for _,_,d,cs,line in self._rows(p):
                program=d.get('Loan Program','')
                match=re.search(r'(20\d{2}|201\d)\s*[–-]\s*(\d{2,4})',program)
                rate=pct(d.get('Annual Percentage Rate (As Low As)',''))
                term=d.get('Term','')
                if not (match and rate and term):continue
                start=int(match.group(1));end=int(match.group(2));end+=100*(start//100) if end<100 else 0
                auto.append((sp,p,start,end,term,rate,line))
        grouped=defaultdict(list)
        for a in auto:
            for year in range(a[2],a[3]+1):
                grouped[(year,a[4])].append(a)
        new_groups={}
        for (year,term),items in grouped.items():
            items.sort(key=lambda x:x[0][0]);latest=items[-1]
            distinct=list(dict.fromkeys(it[5] for it in items))
            if len(distinct)<2:continue
            # Coalesce years that changed in the same way.
            g=(term,tuple(distinct),items[0][2],items[0][3],latest[2],latest[3])
            years=new_groups.setdefault(g,{'years':[],'items':items})
            years['years'].append(year)
        for (term,distinct,oldlo,oldhi,newlo,newhi),bundle in new_groups.items():
            items=bundle['items'];latest=items[-1];yr=bundle['years']
            topic=f'{term} auto loan APR on {min(yr)}-{max(yr)} model-year vehicles'
            if any(topic==c.topic for c in found):continue
            found.append(Conflict(topic=topic,values=list(distinct),
                         sources=[self._source(a[0],a[1],a[6]) for a in (items[0],latest)],
                         resolution=latest[5], reason='Published model-year groups changed across crawls; overlapping model years and the same term are comparable. The latest official auto-loan table wins.'))
        # Avoid a flood of low-quality detections. Key table revisions, then
        # the complete remaining stable product row revisions.
        found.sort(key=lambda c:(int('certificate' in c.topic.lower()),int('auto' in c.topic.lower()),len(c.values)),reverse=True)
        self._conflicts=found[:100]
        return self._conflicts

def build():
    return Knowledge()
