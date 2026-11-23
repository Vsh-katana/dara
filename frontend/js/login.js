const copy = {
  fa: { brandSub:'مدیریت مالی شخصی',secureSpace:'فضای مالی اختصاصی',signIn:'ورود به حساب',intro:'برای مشاهده و مدیریت اطلاعات مالی، وارد حساب خود شوید.',username:'نام کاربری',password:'گذرواژه',noAccount:'حساب ندارید؟',createAccount:'ایجاد حساب',hasAccount:'حساب دارید؟',signUp:'ساخت حساب',isolation:'اطلاعات هر حساب در فضای جداگانه نگهداری می‌شود.',registrationClosed:'ثبت‌نام حساب جدید در حال حاضر بسته است.' },
  en: { brandSub:'Personal finance',secureSpace:'Your private finance space',signIn:'Sign in',intro:'Sign in to view and manage your financial records.',username:'Username',password:'Password',noAccount:'New here?',createAccount:'Create account',hasAccount:'Already have an account?',signUp:'Create account',isolation:'Each account has its own isolated financial records.',registrationClosed:'New account registration is currently closed.' }
};
let lang = localStorage.getItem('dara-language') || 'fa';
let mode = 'login';
const form = document.getElementById('authForm');
const errorBox = document.getElementById('authError');
const submit = document.getElementById('submitButton');
const modeToggle = document.getElementById('modeToggle');
function translate(){
  const strings=copy[lang];
  document.documentElement.lang=lang;
  document.documentElement.dir=lang==='fa'?'rtl':'ltr';
  document.title=`Dara | ${mode==='login'?strings.signIn:strings.signUp}`;
  document.getElementById('authTitle').textContent=mode==='login'?strings.signIn:strings.signUp;
  document.querySelectorAll('[data-copy]').forEach(el=>{el.textContent=strings[el.dataset.copy]||'';});
  document.getElementById('languageToggle').textContent=lang==='fa'?'EN':'FA';
  document.getElementById('switchPrompt').textContent=mode==='login'?strings.noAccount:strings.hasAccount;
  modeToggle.textContent=mode==='login'?strings.createAccount:strings.signIn;
  submit.textContent=mode==='login'?strings.signIn:strings.signUp;
  document.getElementById('password').autocomplete=mode==='login'?'current-password':'new-password';
  errorBox.hidden=true;
}
function setMode(value){mode=value;translate();}
document.getElementById('languageToggle').addEventListener('click',()=>{lang=lang==='fa'?'en':'fa';localStorage.setItem('dara-language',lang);translate();});
modeToggle.addEventListener('click',()=>setMode(mode==='login'?'register':'login'));
form.addEventListener('submit',async event=>{
  event.preventDefault();errorBox.hidden=true;submit.disabled=true;
  const data=new FormData(form);
  try{
    const response=await fetch(`/api/auth/${mode}`,{method:'POST',credentials:'same-origin',cache:'no-store',headers:{'Content-Type':'application/json'},body:JSON.stringify({username:data.get('username'),password:data.get('password')})});
    const result=await response.json();
    if(!response.ok)throw new Error(result.error||`Request failed (${response.status})`);
    location.replace('/');
  }catch(error){errorBox.textContent=error.message;errorBox.hidden=false;}
  finally{submit.disabled=false;}
});
(async()=>{
  translate();
  try{
    const [config,session]=await Promise.all([fetch('/api/auth/config',{cache:'no-store'}),fetch('/api/auth/me',{credentials:'same-origin',cache:'no-store'})]);
    if(session.ok){location.replace('/');return;}
    if(config.ok){const value=await config.json();if(!value.signup_enabled){modeToggle.hidden=true;document.getElementById('switchPrompt').textContent=copy[lang].registrationClosed;}}
  }catch(_){errorBox.textContent='Could not reach the server.';errorBox.hidden=false;}
})();
