import { api, query } from './api.js';

export function todayIso() {
  const now = new Date();
  return `${now.getFullYear()}-${String(now.getMonth()+1).padStart(2,'0')}-${String(now.getDate()).padStart(2,'0')}`;
}

export async function inputDateToIso(value, calendar = 'jalali') {
  const raw = normalizeDigits(String(value || '').trim()).replaceAll('.', '/');
  if (!raw) throw new Error('تاریخ را وارد کنید.');
  const isoLike = /^(\d{4})-(\d{2})-(\d{2})$/.exec(raw);
  if (isoLike && Number(isoLike[1]) > 1700) return raw;
  const result = await api('/date/convert' + query({ value: raw, calendar }));
  return result.iso;
}

export function normalizeDigits(text) {
  return String(text).replace(/[۰-۹٠-٩]/g, ch => {
    const code = ch.charCodeAt(0);
    return code >= 0x06f0 && code <= 0x06f9 ? String(code - 0x06f0) : String(code - 0x0660);
  });
}

export function displayDate(iso, calendar = 'jalali', language = 'fa', options = {}) {
  if (!iso) return '—';
  const date = new Date(`${iso.slice(0,10)}T12:00:00`);
  const locale = language === 'fa' ? 'fa-IR' : 'en-GB';
  const calendarName = calendar === 'jalali' ? 'persian' : 'gregory';
  const digits = language === 'fa' ? 'arabext' : 'latn';
  return new Intl.DateTimeFormat(`${locale}-u-ca-${calendarName}-nu-${digits}`, {
    year: 'numeric', month: options.monthOnly ? 'long' : 'short', ...(options.monthOnly ? {} : { day: 'numeric' })
  }).format(date);
}

export function monthParts(iso, calendar = 'jalali') {
  const date = new Date(`${iso.slice(0,10)}T12:00:00`);
  const parts = new Intl.DateTimeFormat(`en-US-u-ca-${calendar === 'jalali' ? 'persian' : 'gregory'}-nu-latn`, { year:'numeric',month:'numeric' }).formatToParts(date);
  return { year:Number(parts.find(p=>p.type==='year')?.value), month:Number(parts.find(p=>p.type==='month')?.value) };
}

export function monthKey(iso, calendar = 'jalali') {
  const {year,month}=monthParts(iso,calendar);
  return `${year}-${String(month).padStart(2,'0')}`;
}

export async function shiftMonthKey(key, delta, calendar = 'jalali') {
  const [year,month]=key.split('-').map(Number);
  const absolute=year*12+(month-1)+delta;
  const nextYear=Math.floor(absolute/12),nextMonth=(absolute%12)+1;
  if(calendar==='jalali'){
    const converted=await inputDateToIso(`${nextYear}/${String(nextMonth).padStart(2,'0')}/01`,'jalali');
    return monthKey(converted,'jalali');
  }
  return `${nextYear}-${String(nextMonth).padStart(2,'0')}`;
}

export function monthAnchorIso(key, calendar='jalali') {
  const [y,m]=key.split('-').map(Number);
  if(calendar==='gregorian')return `${y}-${String(m).padStart(2,'0')}-01`;
  // The Jalali month is converted by the server in calendar/report APIs; use a Jalali text date in the input.
  return `${y}/${String(m).padStart(2,'0')}/01`;
}

export function weekdayLabels(language='fa') {
  if(language==='fa')return ['ش','ی','د','س','چ','پ','ج'];
  return ['Mon','Tue','Wed','Thu','Fri','Sat','Sun'];
}

export function jalaliDayNumber(iso, calendar='jalali', language='fa') {
  const date=new Date(`${iso}T12:00:00`);
  const parts=new Intl.DateTimeFormat(`en-US-u-ca-${calendar==='jalali'?'persian':'gregory'}-nu-latn`,{day:'numeric'}).formatToParts(date);
  const value=parts.find(p=>p.type==='day')?.value||'';
  return language==='fa'?value.replace(/\d/g,d=>'۰۱۲۳۴۵۶۷۸۹'[Number(d)]):value;
}
