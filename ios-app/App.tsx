/**
 * VIGIL — Passive Health Monitor
 * App.tsx — Production Build v7.1 (Design polish pass)
 *
 * New in v7 (on top of v6):
 * - Multi-person detection UI: identify each person VIGIL sees,
 *   mark one as the resident, others as visitors
 * - Per-person trend screen (PersonDetailScreen) with its own
 *   gait speed / cadence / posture / asymmetry history
 * - Baseline-building progress indicator per identified person
 *
 * Carried over from v6:
 * - Full onboarding flow with AsyncStorage persistence
 * - Activity ring restored with SVG
 * - Camera stream smoothed (no flicker, crossfade)
 * - Fullscreen camera modal
 * - App loads saved profile on every launch
 * - Production-ready: works for any first-time user
 */

import React, {useCallback, useEffect, useRef, useState} from 'react';
import { Share, Linking, NativeModules,
  ActivityIndicator,
  Alert,
  Animated,
  AppState,
  AppStateStatus,
  Dimensions,
  Image,
  Keyboard,
  KeyboardAvoidingView,
  Modal,
  PanResponder,
  Platform,
  RefreshControl,
  SafeAreaView,
  ScrollView,
  StatusBar,
  StyleSheet,
  Switch,
  Text,
  TextInput,
  TouchableOpacity,
  View,
} from 'react-native';
import AsyncStorage from '@react-native-async-storage/async-storage';
import Svg, {Circle, Line, Path} from 'react-native-svg';

let AppleHealthKit: any = null;
let HK_LOAD_ERROR: string | null = null;
try {
  const _hkmod = require('react-native-health');
  AppleHealthKit = _hkmod?.default ?? _hkmod; // handles both CJS and ESM module shapes
  if (!AppleHealthKit) HK_LOAD_ERROR = 'module resolved but exported nothing';
} catch (e: any) {
  HK_LOAD_ERROR = e?.message ?? String(e);
}

import axios from 'axios';
import {launchImageLibrary,launchCamera} from 'react-native-image-picker';  // PHOTO_PICK_V1

// ─────────────────────────────────────────────
// CONFIG
// ─────────────────────────────────────────────
const DEFAULT_IP    = 'your-jetson-ip';
const DEFAULT_PORT  = '5001';
const TIMEOUT_MS    = 10_000;
const AUTO_SYNC_SEC = 60;
const VIGIL_KEY     = 'YOUR_VIGIL_API_KEY';   // must match VIGIL_API_KEY on the monitoring unit
const FALLBACK_URL  = `http://${DEFAULT_IP}:${DEFAULT_PORT}`;

const SK_ONBOARDING = '@vigil_onboarding_v1';
const SK_WATCH_OWNER = '@vigil_watch_owner_v1';   // HOME_PEOPLE_V2: whose watch this phone syncs
const SK_PROFILE    = '@vigil_profile_v1';
const SK_SERVER     = '@vigil_server_v1';

const AX = axios.create({
  timeout: TIMEOUT_MS,
  headers: {'X-VIGIL-Key': VIGIL_KEY, 'Content-Type': 'application/json'},
});

// CLOUD_V1: VIGIL Cloud. One account sees every camera it was granted; each
// camera is its own VIGIL server behind the gateway at <gateway>/s/<camera id>.
// The account's token travels in the same header the Jetson key used, so every
// existing request works unchanged against the cloud.
const SK_CLOUD      = '@vigil_cloud_v1';
const DEFAULT_CLOUD = 'https://api.vigilhealth.me';
type CloudSite = {id:string;name:string;home?:string;room?:string;online?:boolean|null;ntfy_topic?:string};
type CloudCfg  = {gateway:string;token:string;email:string;sites:CloudSite[];siteId:string};
let CLOUD_ACTIVE = false;
function applyCloudHeaders(cfg:CloudCfg|null){
  CLOUD_ACTIVE = !!cfg;
  (AX.defaults.headers as any)['X-VIGIL-Key'] = cfg ? cfg.token : VIGIL_KEY;
}
function cloudSiteUrl(cfg:CloudCfg, siteId?:string){
  return `${cfg.gateway.replace(/\/+$/,'')}/s/${encodeURIComponent(siteId||cfg.siteId)}`;
}

const SW     = Dimensions.get('window').width;
const CAM_W  = SW - 32;
const CAM_H  = Math.round(CAM_W * 9 / 16);
const RING   = SW * 0.52;
const STROKE = 14;
const RADIUS = (RING - STROKE) / 2;
const CIRC   = 2 * Math.PI * RADIUS;

// LIVE-badge pulse — module scope so the loop needs no component hooks/refs
const LIVE_PULSE = new Animated.Value(1);
Animated.loop(
  Animated.sequence([
    Animated.timing(LIVE_PULSE,{toValue:1.15,duration:600,useNativeDriver:true}),
    Animated.timing(LIVE_PULSE,{toValue:1,duration:600,useNativeDriver:true}),
  ]),
).start();

// ─────────────────────────────────────────────
// COLORS
// ─────────────────────────────────────────────
const C = {
  bg:'#050807', card:'#0F1613', cardHi:'#141D19', fill:'#1A2420',
  border:'#24312B', hair:'rgba(255,255,255,0.07)',
  text:'#F2F6F3', sub:'#A3B0A9', muted:'#6C7A73',
  green:'#32D74B', red:'#FF453A', orange:'#FF9F0A',
  blue:'#0A84FF', purple:'#BF5AF2', teal:'#3DD6E3',
};

// ─────────────────────────────────────────────
// TYPES
// ─────────────────────────────────────────────
interface Sample {
  type:string; value:number; unit:string;
  startDate:string; endDate:string; is_daily_total?:boolean;
}
interface DailySummary {
  stride_variability?: number|null;
  date:string; step_count:number|null; hrv_sdnn:number|null;
  resting_hr:number|null; sleep_hours:number|null; active_calories:number|null;
  spo2_avg:number|null; walking_asymmetry_pct:number|null;
  walking_speed_ms:number|null; camera_gait_speed_ms:number|null;
  camera_cadence_spm:number|null; camera_asymmetry_pct:number|null;
  posture_score:number|null; active_minutes:number|null;
  still_minutes:number|null; activity_level_pct:number|null;
  longest_stillness_min:number|null; camera_data_points:number|null;
  anomaly_flag:number;
}
interface CameraStatus {
  online:boolean; fps:number|null; person_present:boolean;
  person_count?:number; last_seen_minutes_ago:number|null; camera_mode:string|null;
}
interface Room {
  id:string; name:string; resident_name:string|null;
  emergency_phone:string|null; camera_online:boolean;
  person_present:boolean; person_count:number; fps:number|null;
}
interface AlertEntry {
  id:number; alert_type:string; detected_at:string;
  payload:any; seen:number; created_at:string;
}
interface MLScore {
  condition_id:string; condition_label:string; score_0_100:number;
  probability:number; published_auc:number; data_quality:number;
  days_of_data:number; insufficient_data:number;
}
interface DebugEntry { ts:string; level:'INFO'|'WARN'|'ERROR'|'OK'; msg:string; }

interface Person {
  id:string; name:string|null; room_id:string; is_resident:number;
  height_px:number|null; confidence:number; frames_seen:number;
  first_seen_at:string|null; last_seen_at:string|null;
  height_cm?:number|null;                                  // HEIGHT_PER_PERSON_V1
}
interface PersonAlert {
  id:number; alert_type:string; room_id:string;
  person_ids:string[]; resolved:number; created_at:string;
}
interface PersonTrendDay {
  date:string; avg_speed:number|null; avg_cadence:number|null;
  avg_posture:number|null; avg_asymmetry:number|null; samples:number;
}
interface PersonBaselineStatus {
  person_id:string; has_baseline:boolean;
  days_collected:number; days_needed:number;
  baseline:{avg_speed?:{mean:number;std:number};avg_cadence?:{mean:number;std:number};
    avg_posture?:{mean:number;std:number};avg_asymmetry?:{mean:number;std:number};}|null;
}

interface SavedProfile {
  residentName:string; residentHeight:string;
  emergencyPhone:string; emergencyEmail:string; watchEnabled:boolean;
  weightKg?:string; age?:string; units?:'us'|'metric';      // PROFILE_V2
}

type MainTab    = 'today'|'trends'|'alerts'|'insights'|'settings';
type SettingTab = 'resident'|'server'|'camera'|'support';

// ─────────────────────────────────────────────
// HELPERS
// ─────────────────────────────────────────────
const fmt = (v:number|null|undefined, d=1):string => {
  if (v==null||isNaN(Number(v))) return '—';
  return Number(v).toFixed(d);
};
const fmtLarge = (v:number|null|undefined):string => {
  if (v==null||isNaN(Number(v))) return '—';
  const n=Number(v);
  return n>=1000?(n/1000).toFixed(1)+'k':Math.round(n).toString();
};
const timeAgo = (iso:string):string => {
  try {
    const m=Math.floor((Date.now()-new Date(iso).getTime())/60000);
    if(m<1)return'just now'; if(m<60)return m+'m ago';
    const h=Math.floor(m/60); return h<24?h+'h ago':Math.floor(h/24)+'d ago';
  } catch { return iso; }
};
const todayRange = () => {
  const now=new Date(); const s=new Date(now); s.setHours(0,0,0,0);
  return {startDate:s.toISOString(),endDate:now.toISOString(),ascending:false,limit:1000};
};
const rollingRange = (hours=168) => {
  const end=new Date(); const start=new Date(end.getTime()-hours*3600000);
  return {startDate:start.toISOString(),endDate:end.toISOString(),ascending:false,limit:500};
};

function readSamples(method:string,opts:object,type:string,unit:string,
  log:(l:DebugEntry['level'],m:string)=>void):Promise<Sample[]> {
  return new Promise(res=>{
    if(!AppleHealthKit)return res([]);
    const fn=(AppleHealthKit as any)[method];
    if(typeof fn!=='function'){return res([]);}
    try{
      fn.call(AppleHealthKit,opts,(err:any,raw:any)=>{
        if(err||!raw)return res([]);
        const arr=Array.isArray(raw)?raw:[raw];
        res(arr.filter((s:any)=>s?.value!=null&&!isNaN(Number(s.value)))
          .map((s:any):Sample=>({
            type,unit,value:Number(s.value),
            startDate:s.startDate??new Date().toISOString(),
            endDate:s.endDate??s.startDate??new Date().toISOString(),
          })));
      });
    }catch(e:any){log('ERROR',`${method}: ${e?.message}`);res([]);}
  });
}

function readDailyTotal(method:string,opts:object,type:string,unit:string,
  log:(l:DebugEntry['level'],m:string)=>void):Promise<Sample[]> {
  return new Promise(res=>{
    if(!AppleHealthKit)return res([]);
    const fn=(AppleHealthKit as any)[method];
    if(typeof fn!=='function')return res([]);
    try{
      fn.call(AppleHealthKit,opts,(err:any,raw:any)=>{
        if(err||raw==null)return res([]);
        const arr=Array.isArray(raw)?raw:[raw];
        const total=arr.reduce((s:number,x:any)=>s+Number(x?.value??x??0),0);
        if(!total||isNaN(total))return res([]);
        const m=new Date();m.setHours(0,0,0,0);
        res([{type,unit,value:total,startDate:m.toISOString(),endDate:new Date().toISOString(),is_daily_total:true}]);
      });
    }catch{res([]);}
  });
}

// ─────────────────────────────────────────────
// ONBOARDING
// ─────────────────────────────────────────────
interface OBProps { onComplete:(profile:SavedProfile,serverIp:string,serverPort:string)=>void; onDemo?:()=>void; }

// ACCOUNTS_V1: create an account, sign in, or reset a forgotten password.
// A new account has no cameras yet; AddCameraCard adds one by its setup code.
function CloudSignIn({onSignedIn,startMode='signup'}:{onSignedIn:(cfg:CloudCfg)=>void;startMode?:'signup'|'signin'}){
  const [mode,setMode]=useState<'signup'|'signin'|'forgot'|'reset'|'verify'>(startMode);
  const [pending,setPending]=useState<any>(null);   // VERIFY_EMAIL_V1: signed in, email not confirmed yet
  const [gateway,setGateway]=useState(DEFAULT_CLOUD);
  const [adv,setAdv]=useState(false);
  const [name,setName]=useState('');
  const [email,setEmail]=useState('');
  const [phone,setPhone]=useState('');
  const [password,setPassword]=useState('');
  const [code,setCode]=useState('');
  const [busy,setBusy]=useState(false);
  const [err,setErr]=useState<string|null>(null);
  const [note,setNote]=useState<string|null>(null);
  const [showPw,setShowPw]=useState(false);
  const gw=()=>gateway.trim().replace(/\/+$/,'');
  const fail=(e:any,fallback:string)=>setErr(e?.response?.data?.reason||
    (e?.message?.includes('Network')?'Cannot reach VIGIL. Check the internet connection and try again.':fallback));
  const done=(d:any)=>{
    if(d?.user&&d.user.email_verified===false&&mode!=='verify'){
      setPending(d);setMode('verify');setCode('');setErr(null);
      setNote(mode==='signup'?`We emailed a 6-digit code to ${email.trim()}. Type it here to confirm your email.`
        :`Your email is not confirmed yet. We sent a new 6-digit code to ${email.trim()}.`);
      if(mode!=='signup')axios.post(`${gw()}/auth/verify/resend`,{},{timeout:15000,headers:{'X-VIGIL-Key':d.token}}).catch(()=>{});
      return;
    }
    const sites:CloudSite[]=d?.sites||[];
    onSignedIn({gateway:gw(),token:d.token,email:email.trim().toLowerCase(),sites,siteId:sites[0]?.id||''});
    setPassword('');
  };
  const go=async()=>{
    setBusy(true);setErr(null);setNote(null);
    try{
      if(mode==='signup'){
        const r=await axios.post(`${gw()}/auth/signup`,{name:name.trim(),email:email.trim(),phone:phone.trim(),password,device:Platform.OS},{timeout:15000});
        done(r.data);
      }else if(mode==='signin'){
        const r=await axios.post(`${gw()}/auth/login`,{email:email.trim(),password,device:Platform.OS},{timeout:15000});
        done(r.data);
      }else if(mode==='forgot'){
        const r=await axios.post(`${gw()}/auth/forgot`,{email:email.trim()},{timeout:15000});
        if(r.data?.email_enabled===false){setErr('Password emails are not set up yet. Contact VIGIL support.');return;}
        setNote(`If ${email.trim()} has a VIGIL account, a 6-digit code is on its way. Check your email (and spam).`);
        setMode('reset');
      }else if(mode==='verify'){
        await axios.post(`${gw()}/auth/verify`,{code:code.trim()},{timeout:15000,headers:{'X-VIGIL-Key':pending.token}});
        done(pending);
      }else{
        const r=await axios.post(`${gw()}/auth/reset`,{email:email.trim(),code:code.trim(),new_password:password,device:Platform.OS},{timeout:15000});
        done(r.data);
      }
    }catch(e:any){
      if(mode==='signup'&&e?.response?.status===409){   // already has an account: take them to sign in, email kept
        setMode('signin');setPassword('');setErr(null);setNote('You already have a VIGIL account with this email. Sign in below.');return;}
      fail(e,mode==='signup'?'Could not create the account.':mode==='signin'?'Sign-in failed.':'Something went wrong. Try again.');}
    finally{setBusy(false);}
  };
  const ready=mode==='signup'?(!!name.trim()&&!!email.trim()&&password.length>=8)
    :mode==='signin'?(!!email.trim()&&!!password)
    :mode==='forgot'?!!email.trim()
    :mode==='verify'?code.trim().length===6
    :(code.trim().length===6&&password.length>=8);
  const inp={color:C.text,fontSize:17,paddingHorizontal:14,paddingVertical:14,backgroundColor:C.bg,borderRadius:10,borderWidth:1,borderColor:C.border,marginTop:8};
  const title={signup:'Create your account',signin:'Sign in',forgot:'Forgot your password?',reset:'Choose a new password',verify:'Confirm your email'}[mode];
  const sub={signup:'You will use this email and password to sign in on any phone.',
    signin:'Use the email and password of your VIGIL account.',
    forgot:'Type your email and we will send you a 6-digit code.',
    reset:'Type the 6-digit code from the email and a new password.',
    verify:'Fall alerts are only emailed to confirmed addresses. Check your inbox (and spam).'}[mode];
  const btn={signup:'Create account',signin:'Sign in',forgot:'Email me a code',reset:'Save and sign in',verify:'Confirm'}[mode];
  return(
    <View style={[s.card,{marginHorizontal:16,marginTop:16}]}>
      <Text style={{color:C.text,fontSize:20,fontWeight:'700'}}>{title}</Text>
      <Text style={{color:C.sub,fontSize:15,marginTop:6,lineHeight:21}}>{sub}</Text>
      {note&&<Text style={{color:C.green,fontSize:14,marginTop:10,lineHeight:20}}>{note}</Text>}
      {mode==='signup'&&<TextInput style={inp} value={name} onChangeText={setName} placeholder="Your name" placeholderTextColor={C.muted}
        autoCapitalize="words" autoCorrect={false} textContentType="name" returnKeyType="next"/>}
      {mode!=='reset'&&mode!=='verify'&&<TextInput style={inp} value={email} onChangeText={setEmail} placeholder="Email" placeholderTextColor={C.muted}
        autoCapitalize="none" autoCorrect={false} keyboardType="email-address" textContentType={mode==='signup'?'emailAddress':'username'} returnKeyType="next"/>}
      {mode==='signup'&&<TextInput style={inp} value={phone} onChangeText={setPhone} placeholder="Mobile number (optional)" placeholderTextColor={C.muted}
        autoCorrect={false} keyboardType="phone-pad" textContentType="telephoneNumber"/>}
      {(mode==='reset'||mode==='verify')&&<TextInput style={inp} value={code} onChangeText={t=>setCode(t.replace(/[^0-9]/g,'').slice(0,6))} placeholder="6-digit code"
        placeholderTextColor={C.muted} keyboardType="number-pad" textContentType="oneTimeCode"/>}
      {mode!=='forgot'&&mode!=='verify'&&<TextInput style={inp} value={password} onChangeText={setPassword}
        placeholder={mode==='signin'?'Password':'Password (at least 8 characters)'} placeholderTextColor={C.muted}
        autoCapitalize="none" autoCorrect={false} secureTextEntry={!showPw} textContentType={mode==='signin'?'password':'newPassword'}
        returnKeyType="go" onSubmitEditing={()=>{if(ready)go();}}/>}
      {mode!=='forgot'&&mode!=='verify'&&<TouchableOpacity onPress={()=>setShowPw(v=>!v)} style={{alignSelf:'flex-end',paddingTop:8}}>
        <Text style={{color:C.sub,fontSize:13}}>{showPw?'Hide password':'Show password'}</Text></TouchableOpacity>}
      {adv&&<TextInput style={[inp,{fontSize:13,color:C.sub}]} value={gateway} onChangeText={setGateway} placeholder={DEFAULT_CLOUD}
        placeholderTextColor={C.muted} autoCapitalize="none" autoCorrect={false} keyboardType="url"/>}
      {err&&<Text style={{color:C.red,fontSize:14,marginTop:10,lineHeight:20}}>{err}</Text>}
      <TouchableOpacity style={[s.btnPrimary,{marginTop:14,opacity:(busy||!ready)?0.5:1}]} disabled={busy||!ready} onPress={go}>
        {busy?<ActivityIndicator color={C.bg}/>:<Text style={[s.btnPrimaryTxt,{fontSize:17}]}>{btn}</Text>}
      </TouchableOpacity>
      {mode==='signup'&&<TouchableOpacity onPress={()=>{setMode('signin');setErr(null);}} style={{alignItems:'center',paddingTop:16}}>
        <Text style={{color:C.blue,fontSize:16}}>I already have an account</Text></TouchableOpacity>}
      {mode==='signin'&&<>
        <TouchableOpacity onPress={()=>{setMode('forgot');setErr(null);}} style={{alignItems:'center',paddingTop:16}}>
          <Text style={{color:C.blue,fontSize:16}}>Forgot password?</Text></TouchableOpacity>
        <TouchableOpacity onPress={()=>{setMode('signup');setErr(null);}} style={{alignItems:'center',paddingTop:12}}>
          <Text style={{color:C.sub,fontSize:15}}>New to VIGIL? Create an account</Text></TouchableOpacity>
      </>}
      {mode==='verify'&&<>
        <TouchableOpacity disabled={busy} onPress={()=>{setErr(null);axios.post(`${gw()}/auth/verify/resend`,{},{timeout:15000,headers:{'X-VIGIL-Key':pending?.token}})
            .then(()=>setNote(`A new code is on its way to ${email.trim()}.`)).catch((e:any)=>fail(e,'Could not send a new code.'));}}
          style={{alignItems:'center',paddingTop:16}}><Text style={{color:C.blue,fontSize:16}}>Send a new code</Text></TouchableOpacity>
        <TouchableOpacity onPress={()=>{if(pending){const d={...pending,user:{...pending.user,email_verified:true}};setPending(null);onSignedIn({gateway:gw(),token:d.token,email:email.trim().toLowerCase(),sites:d.sites||[],siteId:d.sites?.[0]?.id||''});}}}
          style={{alignItems:'center',paddingTop:12}}><Text style={{color:C.sub,fontSize:15}}>Do this later (no alert emails until then)</Text></TouchableOpacity>
      </>}
      {(mode==='forgot'||mode==='reset')&&<TouchableOpacity onPress={()=>{setMode('signin');setErr(null);setNote(null);}} style={{alignItems:'center',paddingTop:16}}>
        <Text style={{color:C.sub,fontSize:15}}>Back to sign in</Text></TouchableOpacity>}
      <TouchableOpacity onPress={()=>setAdv(v=>!v)} style={{alignSelf:'center',paddingTop:14}}>
        <Text style={{color:C.muted,fontSize:12}}>{adv?'Hide server address':'Server address'}</Text>
      </TouchableOpacity>
    </View>
  );
}

// ACCOUNTS_V1: add a camera to the account with the code printed on its card.
function AddCameraCard({cfg,onAdded,onSkip,initialCode=''}:{cfg:CloudCfg;onAdded:(cfg:CloudCfg,role?:string)=>void;onSkip?:()=>void;initialCode?:string}){
  const fmt=(t:string)=>{const c=t.toUpperCase().replace(/[^A-Z0-9]/g,'').slice(0,8);return c.length>4?c.slice(0,4)+'-'+c.slice(4):c;};
  const [code,setCode]=useState(fmt(initialCode));
  const [busy,setBusy]=useState(false);
  const [err,setErr]=useState<string|null>(null);
  const clean=code.toUpperCase().replace(/[^A-Z0-9]/g,'');
  const add=async()=>{
    setBusy(true);setErr(null);
    try{
      const r=await axios.post(`${cfg.gateway}/auth/claim`,{code:clean},{timeout:15000,headers:{'X-VIGIL-Key':cfg.token}});
      const sites:CloudSite[]=r.data?.sites||[];
      onAdded({...cfg,sites,siteId:r.data?.site?.id||sites[0]?.id||cfg.siteId},r.data?.role);
      setCode('');
    }catch(e:any){setErr(e?.response?.data?.reason||'Could not add the camera. Check the internet connection.');}
    finally{setBusy(false);}
  };
  const auto=useRef(false);
  useEffect(()=>{if(!auto.current&&clean.length===8&&initialCode){auto.current=true;add();}},[]);   // scanned from the card: no typing
  return(
    <View style={[s.card,{marginHorizontal:16,marginTop:16}]}>
      <Text style={{color:C.text,fontSize:20,fontWeight:'700'}}>Add a camera</Text>
      <Text style={{color:C.sub,fontSize:15,marginTop:6,lineHeight:21}}>Type the 8-character setup code printed on the card that came with the VIGIL camera.</Text>
      <TextInput style={{color:C.text,fontSize:26,letterSpacing:4,textAlign:'center',paddingVertical:16,backgroundColor:C.bg,
          borderRadius:10,borderWidth:1,borderColor:C.border,marginTop:14,fontWeight:'700'}}
        value={code} onChangeText={t=>{const c=t.toUpperCase().replace(/[^A-Z0-9]/g,'').slice(0,8);setCode(c.length>4?c.slice(0,4)+'-'+c.slice(4):c);}}
        placeholder="ABCD-1234" placeholderTextColor={C.muted} autoCapitalize="characters" autoCorrect={false} returnKeyType="go"
        onSubmitEditing={()=>{if(clean.length===8)add();}}/>
      {err&&<Text style={{color:C.red,fontSize:14,marginTop:10,lineHeight:20}}>{err}</Text>}
      <TouchableOpacity style={[s.btnPrimary,{marginTop:14,opacity:(busy||clean.length!==8)?0.5:1}]} disabled={busy||clean.length!==8} onPress={add}>
        {busy?<ActivityIndicator color={C.bg}/>:<Text style={[s.btnPrimaryTxt,{fontSize:17}]}>Add camera</Text>}
      </TouchableOpacity>
      {onSkip&&<TouchableOpacity onPress={onSkip} style={{alignItems:'center',paddingTop:16}}>
        <Text style={{color:C.sub,fontSize:15}}>I will do this later</Text></TouchableOpacity>}
    </View>
  );
}

// ACCOUNTS_V1: the signed-in person's details and alert emails (Settings > Account).
function AccountSection({cfg}:{cfg:CloudCfg}){
  const [user,setUser]=useState<any>(null);
  const [name,setName]=useState('');
  const [phone,setPhone]=useState('');
  const [msg,setMsg]=useState<string|null>(null);
  const [busy,setBusy]=useState(false);
  const h={timeout:12000,headers:{'X-VIGIL-Key':cfg.token}};
  useEffect(()=>{
    axios.get(`${cfg.gateway}/auth/profile`,h).then(r=>{const u=r.data?.user;setUser(u);setName(u?.name||'');setPhone(u?.phone||'');}).catch(()=>{});
  },[cfg.token]);
  const save=async(patch:any,okMsg:string)=>{
    setBusy(true);setMsg(null);
    try{const r=await axios.post(`${cfg.gateway}/auth/profile`,patch,h);setUser(r.data?.user);setMsg(okMsg);}
    catch(e:any){setMsg(e?.response?.data?.reason||'Could not save. Check the internet connection.');}
    finally{setBusy(false);}
  };
  const testEmail=async()=>{
    setBusy(true);setMsg(null);
    try{const r=await axios.post(`${cfg.gateway}/auth/test-email`,{},h);setMsg(r.data?.status==='ok'?`Test email sent to ${cfg.email}. Check your inbox (and spam).`:(r.data?.reason||'The email could not be sent.'));}
    catch(e:any){setMsg(e?.response?.data?.reason||'The email could not be sent.');}
    finally{setBusy(false);}
  };
  const [vcode,setVcode]=useState('');
  const [vsent,setVsent]=useState(false);
  const verify=async()=>{
    setBusy(true);setMsg(null);
    try{const r=await axios.post(`${cfg.gateway}/auth/verify`,{code:vcode},h);setUser(r.data?.user);setVcode('');setMsg('Email confirmed. Alert emails will come here.');}
    catch(e:any){setMsg(e?.response?.data?.reason||'Could not confirm. Check the internet connection.');}
    finally{setBusy(false);}
  };
  const resend=async()=>{
    setBusy(true);setMsg(null);
    try{await axios.post(`${cfg.gateway}/auth/verify/resend`,{},h);setVsent(true);setMsg(`Code sent to ${cfg.email}.`);}
    catch(e:any){setMsg(e?.response?.data?.reason||'Could not send the code.');}
    finally{setBusy(false);}
  };
  const inp={color:C.text,fontSize:16,paddingHorizontal:12,paddingVertical:12,backgroundColor:C.bg,borderRadius:10,borderWidth:1,borderColor:C.border,marginTop:6};
  if(!user)return null;
  const changed=name.trim()!==(user.name||'')||phone.trim()!==(user.phone||'');
  return(
    <View style={[s.card,{marginHorizontal:16,marginTop:16}]}>
      <Text style={{color:C.text,fontSize:17,fontWeight:'700'}}>Your account</Text>
      <Text style={{color:C.sub,fontSize:13,marginTop:4}}>{cfg.email}{user.role?` · ${user.role==='owner'?'Owner':'Caregiver'}`:''}</Text>
      {user.email_verified===false&&(
        <View style={{marginTop:12,padding:12,borderRadius:10,backgroundColor:C.orange+'18',borderWidth:1,borderColor:C.orange}}>
          <Text style={{color:C.text,fontSize:14,fontWeight:'700'}}>Confirm your email to get alert emails</Text>
          {vsent?<>
            <TextInput style={inp} value={vcode} onChangeText={t=>setVcode(t.replace(/[^0-9]/g,'').slice(0,6))} placeholder="6-digit code"
              placeholderTextColor={C.muted} keyboardType="number-pad" textContentType="oneTimeCode"/>
            <TouchableOpacity style={[s.btnPrimary,{marginTop:10,opacity:(busy||vcode.length!==6)?0.5:1}]} disabled={busy||vcode.length!==6} onPress={verify}>
              <Text style={s.btnPrimaryTxt}>Confirm</Text></TouchableOpacity>
            <TouchableOpacity onPress={resend} disabled={busy} style={{paddingTop:10}}><Text style={{color:C.blue,fontSize:14}}>Send a new code</Text></TouchableOpacity>
          </>:<TouchableOpacity onPress={resend} disabled={busy} style={{paddingTop:8}}><Text style={{color:C.blue,fontSize:15}}>Email me a code</Text></TouchableOpacity>}
        </View>
      )}
      <Text style={{color:C.sub,fontSize:13,marginTop:14}}>Name</Text>
      <TextInput style={inp} value={name} onChangeText={setName} autoCapitalize="words" placeholder="Your name" placeholderTextColor={C.muted}/>
      <Text style={{color:C.sub,fontSize:13,marginTop:10}}>Mobile number</Text>
      <TextInput style={inp} value={phone} onChangeText={setPhone} keyboardType="phone-pad" placeholder="Optional" placeholderTextColor={C.muted}/>
      {changed&&<TouchableOpacity style={[s.btnPrimary,{marginTop:12,opacity:busy?0.5:1}]} disabled={busy}
        onPress={()=>save({name:name.trim(),phone:phone.trim()},'Saved.')}>
        <Text style={s.btnPrimaryTxt}>Save</Text></TouchableOpacity>}
      <View style={{flexDirection:'row',alignItems:'center',marginTop:18,paddingTop:14,borderTopWidth:1,borderTopColor:C.hair}}>
        <View style={{flex:1,paddingRight:10}}>
          <Text style={{color:C.text,fontSize:15,fontWeight:'600'}}>Email me alerts</Text>
          <Text style={{color:C.sub,fontSize:12,marginTop:2,lineHeight:17}}>Falls, time on the floor, and when a camera goes offline, sent to {cfg.email}.</Text>
        </View>
        <Switch value={!!user.email_alerts} disabled={busy} onValueChange={v=>save({email_alerts:v},v?'Alert emails are on.':'Alert emails are off.')}
          trackColor={{false:C.fill,true:C.green}} thumbColor="#FFFFFF"/>
      </View>
      <TouchableOpacity onPress={testEmail} disabled={busy} style={{paddingTop:14}}>
        <Text style={{color:C.blue,fontSize:15}}>Send me a test email</Text></TouchableOpacity>
      {msg&&<Text style={{color:C.sub,fontSize:13,marginTop:10,lineHeight:18}}>{msg}</Text>}
    </View>
  );
}

// APP_SETUP_V1 / SETUP_DELIGHT_V1: the family sets up the box and camera from the
// VIGIL app alone, the way a new AirPods or iPhone pairs: the device is drawn and
// moves, its light in the app blinks exactly like the real one, the phone joins
// the box with one tap, a "found your box" card slides up, every connection
// lights up as it really happens, and it ends on their own room, live.
// The Tapo app is only used to put the camera on Wi-Fi (TP-Link allows nothing
// else); VIGIL hands them the exact camera account to type there.
const BOX_URL='http://10.42.0.1';
const TAPO_APP='https://apps.apple.com/app/tp-link-tapo/id1472718009';
const Hotspot:any=(NativeModules as any).VIGILHotspot;
function genCamPass(){const a='ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789';let p='';for(let i=0;i<12;i++)p+=a[Math.floor(Math.random()*a.length)];return p;}

type Led='double'|'slow'|'fast'|'steady'|'off';
// The light on the drawing blinks in the same pattern as the real box.
function useLed(mode:Led){
  const v=useRef(new Animated.Value(mode==='steady'?1:0)).current;
  useEffect(()=>{
    const on=(d:number)=>Animated.timing(v,{toValue:1,duration:d,useNativeDriver:true});
    const off=(d:number)=>Animated.timing(v,{toValue:0.08,duration:d,useNativeDriver:true});
    const w=(d:number)=>Animated.delay(d);
    let seq:Animated.CompositeAnimation|null=null;
    if(mode==='double')seq=Animated.loop(Animated.sequence([on(80),w(120),off(80),w(120),on(80),w(120),off(80),w(800)]));
    else if(mode==='slow')seq=Animated.loop(Animated.sequence([on(200),w(800),off(200),w(800)]));
    else if(mode==='fast')seq=Animated.loop(Animated.sequence([on(60),w(180),off(60),w(180)]));
    else v.setValue(mode==='steady'?1:0.08);
    seq?.start();return()=>seq?.stop();
  },[mode]);
  return v;
}
function BoxArt({size=150,led='double'}:{size?:number;led?:Led}){
  const glow=useLed(led);
  const w=size,k=size/150,h=111*k;
  return(
    <View style={{width:w,height:h}}>
      <Svg width={w} height={h} viewBox="0 0 150 111">
        <Path d="M14 96 Q75 108 136 96" stroke="#000" strokeOpacity={0.5} strokeWidth={10} fill="none" strokeLinecap="round"/>
        <Path d="M12 22 Q12 10 24 10 L126 10 Q138 10 138 22 L138 82 Q138 94 126 94 L24 94 Q12 94 12 82 Z" fill="#1C1C21"/>
        <Path d="M12 22 Q12 10 24 10 L126 10 Q138 10 138 22 L138 30 L12 30 Z" fill="#26262D"/>
        <Path d="M60 44 L75 72 L90 44" stroke="#32D74B" strokeWidth={7} fill="none" strokeLinecap="round" strokeLinejoin="round"/>
        <Circle cx={118} cy={80} r={4} fill="#0E3A16"/>
      </Svg>
      <Animated.View style={{position:'absolute',left:118*k-5*k,top:80*k-5*k,width:10*k,height:10*k,borderRadius:5*k,
        backgroundColor:'#32D74B',opacity:glow,shadowColor:'#32D74B',shadowOpacity:1,shadowRadius:7*k,shadowOffset:{width:0,height:0}}}/>
    </View>);
}
function CamArt({size=110}:{size?:number}){
  return(
    <Svg width={size} height={size*1.15} viewBox="0 0 100 115">
      <Path d="M22 108 Q50 116 78 108" stroke="#000" strokeOpacity={0.5} strokeWidth={8} fill="none" strokeLinecap="round"/>
      <Path d="M26 92 Q26 84 34 84 L66 84 Q74 84 74 92 L74 100 Q74 104 70 104 L30 104 Q26 104 26 100 Z" fill="#E9E9EE"/>
      <Path d="M44 70 L56 70 L58 86 L42 86 Z" fill="#D4D4DA"/>
      <Circle cx={50} cy={44} r={34} fill="#F4F4F7"/>
      <Path d="M18 38 Q50 22 82 38 L82 52 Q50 66 18 52 Z" fill="#141418"/>
      <Circle cx={50} cy={45} r={11} fill="#232329"/>
      <Circle cx={50} cy={45} r={6.5} fill="#0A84FF" fillOpacity={0.55}/>
      <Circle cx={47} cy={42} r={2} fill="#FFFFFF" fillOpacity={0.8}/>
      <Circle cx={70} cy={45} r={2.2} fill="#32D74B"/>
    </Svg>);
}
// Gentle turntable motion: the device turns a little and floats, like a product page.
function Turntable({children,amount=22,still=false}:{children:any;amount?:number;still?:boolean}){
  const t=useRef(new Animated.Value(0)).current;
  const intro=useRef(new Animated.Value(0)).current;
  useEffect(()=>{
    Animated.spring(intro,{toValue:1,friction:6,tension:60,useNativeDriver:true}).start();
    if(still)return;
    const loop=Animated.loop(Animated.sequence([
      Animated.timing(t,{toValue:1,duration:2600,useNativeDriver:true}),
      Animated.timing(t,{toValue:0,duration:2600,useNativeDriver:true})]));
    loop.start();return()=>loop.stop();
  },[still]);
  return(
    <Animated.View style={{opacity:intro,transform:[{perspective:700},
      {scale:intro.interpolate({inputRange:[0,1],outputRange:[0.6,1]})},
      {rotateY:t.interpolate({inputRange:[0,1],outputRange:[`-${amount}deg`,`${amount}deg`]})},
      {translateY:t.interpolate({inputRange:[0,0.5,1],outputRange:[0,-6,0]})}]}}>
      {children}
    </Animated.View>);
}
// Expanding rings behind the success check (the peak moment).
function Burst({color=C.green}:{color?:string}){
  const rings=[useRef(new Animated.Value(0)).current,useRef(new Animated.Value(0)).current,useRef(new Animated.Value(0)).current];
  const pop=useRef(new Animated.Value(0)).current;
  useEffect(()=>{
    Animated.spring(pop,{toValue:1,friction:4,tension:80,useNativeDriver:true}).start();
    rings.forEach((r,i)=>Animated.sequence([Animated.delay(i*180),Animated.timing(r,{toValue:1,duration:1100,useNativeDriver:true})]).start());
  },[]);
  return(
    <View style={{width:180,height:180,alignItems:'center',justifyContent:'center'}}>
      {rings.map((r,i)=>(
        <Animated.View key={i} style={{position:'absolute',width:96,height:96,borderRadius:48,borderWidth:3,borderColor:color,
          opacity:r.interpolate({inputRange:[0,1],outputRange:[0.7,0]}),transform:[{scale:r.interpolate({inputRange:[0,1],outputRange:[1,2]})}]}}/>))}
      <Animated.View style={{width:96,height:96,borderRadius:48,backgroundColor:color,alignItems:'center',justifyContent:'center',transform:[{scale:pop}]}}>
        <Svg width={52} height={52} viewBox="0 0 52 52"><Path d="M12 27 L22 37 L41 16" stroke="#000" strokeWidth={6} fill="none" strokeLinecap="round" strokeLinejoin="round"/></Svg>
      </Animated.View>
    </View>);
}
// One link in the live connection chain: a node that lights up when it is truly reached.
function ChainNode({label,detail,state}:{label:string;detail?:string;state:'wait'|'work'|'ok'|'bad'}){
  const pulse=useRef(new Animated.Value(0)).current;
  useEffect(()=>{
    if(state!=='work'){pulse.setValue(0);return;}
    const l=Animated.loop(Animated.sequence([Animated.timing(pulse,{toValue:1,duration:700,useNativeDriver:true}),Animated.timing(pulse,{toValue:0,duration:700,useNativeDriver:true})]));
    l.start();return()=>l.stop();
  },[state]);
  const col=state==='ok'?C.green:state==='bad'?C.orange:state==='work'?C.text:C.muted;
  return(
    <View style={{flexDirection:'row',alignItems:'center',paddingVertical:10}}>
      <Animated.View style={{width:30,height:30,borderRadius:15,alignItems:'center',justifyContent:'center',marginRight:14,
        backgroundColor:state==='ok'?C.green:'transparent',borderWidth:state==='ok'?0:2,borderColor:col,
        opacity:state==='work'?pulse.interpolate({inputRange:[0,1],outputRange:[0.45,1]}):1}}>
        {state==='ok'&&<Text style={{color:'#000',fontWeight:'900',fontSize:15}}>{'✓'}</Text>}
        {state==='bad'&&<Text style={{color:C.orange,fontWeight:'900',fontSize:15}}>!</Text>}
      </Animated.View>
      <View style={{flex:1}}>
        <Text style={{color:state==='wait'?C.muted:C.text,fontSize:16,fontWeight:'700'}}>{label}</Text>
        {!!detail&&<Text style={{color:C.sub,fontSize:13,marginTop:2}}>{detail}</Text>}
      </View>
    </View>);
}

function BoxSetup({cfg,onDone,onSkip}:{cfg:CloudCfg;onDone:()=>void;onSkip:()=>void}){
  const STEPS=['plug','tapo','connect','live'];
  const [step,setStep]=useState(0);
  const [camPass,setCamPass]=useState('');
  const [code,setCode]=useState('');
  const [tapoDone,setTapoDone]=useState(false);
  const [box,setBox]=useState<any>(null);
  const [foundAt,setFoundAt]=useState(0);
  const [joinTried,setJoinTried]=useState(false);
  const [joinFailed,setJoinFailed]=useState(false);
  const [nets,setNets]=useState<{ssid:string;signal:number}[]>([]);
  const [ssid,setSsid]=useState('');
  const [manual,setManual]=useState(false);
  const [wpw,setWpw]=useState('');
  const [showPw,setShowPw]=useState(false);
  const [busy,setBusy]=useState(false);
  const [err,setErr]=useState<string|null>(null);
  const [liveS,setLiveS]=useState(0);
  const [relay,setRelay]=useState<any>(null);
  const [first,setFirst]=useState<string|null>(null);
  const [done,setDone]=useState(false);
  const sheet=useRef(new Animated.Value(0)).current;
  const fadeIn=useRef(new Animated.Value(0)).current;
  const KEY='vigil_box_setup_'+cfg.siteId;
  const site=cfg.sites.find(x=>x.id===cfg.siteId);
  const boxSsid=`VIGIL-${cfg.siteId}`;
  const boxPw=code.replace(/[^A-Za-z0-9]/g,'').toUpperCase();
  const cur=STEPS[step];
  const gh={timeout:8000,headers:{'X-VIGIL-Key':cfg.token}};
  useEffect(()=>{
    applyCloudHeaders(cfg);
    AsyncStorage.getItem(KEY).then(v=>{const d=v?JSON.parse(v):{};setCamPass(d.camPass||genCamPass());if(d.step)setStep(Math.min(d.step,3));if(d.tapoDone)setTapoDone(true);}).catch(()=>setCamPass(genCamPass()));
    axios.get(`${cfg.gateway}/auth/site/${cfg.siteId}/members`,gh).then(r=>setCode(r.data?.setup_code||'')).catch(()=>{});
  },[]);
  const save=(patch:any)=>AsyncStorage.getItem(KEY).then(v=>AsyncStorage.setItem(KEY,JSON.stringify({...(v?JSON.parse(v):{}),camPass,...patch}))).catch(()=>{});
  const go=(n:number)=>{setErr(null);setStep(n);save({step:n,camPass,tapoDone});};
  // find the box while the phone is on its Wi-Fi
  useEffect(()=>{
    if(cur!=='connect')return;
    let alive=true;
    (async()=>{while(alive){
      try{const r=await axios.get(`${BOX_URL}/api/info`,{timeout:2500});
        if(alive&&r.data?.box==='vigil-relay'){
          setBox((b:any)=>{if(!b){setFoundAt(Date.now());Animated.spring(sheet,{toValue:1,friction:8,tension:55,useNativeDriver:true}).start();}return r.data;});
          if(!nets.length){try{const n=await axios.get(`${BOX_URL}/api/networks`,{timeout:4000});if(alive){const l=n.data?.networks||[];setNets(l);if(l[0]&&!ssid)setSsid(l[0].ssid);}}catch(_e){}}}}
      catch(_e){}
      await new Promise<void>(res=>setTimeout(()=>res(),1500));
    }})();
    return()=>{alive=false;};
  },[cur]);
  // SETUP_RESUME_V1: the box may already be on the home Wi-Fi (setup sent, or done
  // before). If the cloud hears from it, skip straight to the live progress.
  useEffect(()=>{
    if(cur!=='connect')return;
    let alive=true;
    (async()=>{while(alive){
      try{const r=await axios.get(`${cfg.gateway}/auth/site/${cfg.siteId}/relay`,gh);
        if(alive&&r.data?.box_online&&r.data?.state!=='needs_camera_account'){setLiveS(0);setRelay(r.data);setDone(false);go(3);break;}}catch(_e){}
      await new Promise<void>(res=>setTimeout(()=>res(),4000));
    }})();
    return()=>{alive=false;};
  },[cur]);
  const joinBox=async()=>{
    setJoinTried(true);setJoinFailed(false);setErr(null);
    if(!Hotspot?.join||!boxPw){setJoinFailed(true);return;}
    try{await Hotspot.join(boxSsid,boxPw);}
    catch(_e){setJoinFailed(true);}
  };
  // live progress from the box itself, then the first picture
  useEffect(()=>{
    if(cur!=='live')return;
    let alive=true;let n=0;
    (async()=>{while(alive){
      try{const r=await axios.get(`${cfg.gateway}/auth/site/${cfg.siteId}/relay`,gh);if(alive)setRelay(r.data);
        if(r.data?.site_online){
          try{const f=await AX.get(`${cloudSiteUrl(cfg)}/camera/snapshot?w=800`,{timeout:8000});if(alive&&f.data?.frame)setFirst(f.data.frame);}catch(_e){}
          if(alive){setDone(true);AsyncStorage.removeItem(KEY).catch(()=>{});Animated.timing(fadeIn,{toValue:1,duration:900,delay:500,useNativeDriver:true}).start();}
          break;}}catch(_e){}
      n+=3;if(alive)setLiveS(n);
      await new Promise<void>(res=>setTimeout(()=>res(),3000));
    }})();
    return()=>{alive=false;};
  },[cur]);
  const send=async()=>{
    setBusy(true);setErr(null);
    try{
      await axios.post(`${BOX_URL}/api/setup`,{code:boxPw,ssid:ssid.trim(),wifi_password:wpw,tapo_user:'vigil',tapo_pass:camPass},{timeout:8000});
      try{Hotspot?.leave?.(boxSsid);}catch(_e){}
      setLiveS(0);setRelay(null);setDone(false);go(3);
    }catch(e:any){setErr(e?.response?.data?.reason||`Could not reach the box. Check this phone is still on ${boxSsid}.`);}
    finally{setBusy(false);}
  };
  const copy=(t:string)=>Share.share({message:t}).catch(()=>{});
  const Title=({t,sub}:{t:string;sub?:string})=>(<View style={{alignItems:'center',marginTop:6}}>
    <Text style={{color:C.text,fontSize:28,fontWeight:'800',letterSpacing:-0.4,textAlign:'center'}}>{t}</Text>
    {!!sub&&<Text style={{color:C.sub,fontSize:16,marginTop:8,lineHeight:23,textAlign:'center',maxWidth:340}}>{sub}</Text>}</View>);
  const Field=({label,value}:{label:string;value:string})=>(
    <TouchableOpacity onPress={()=>copy(value)} style={{flexDirection:'row',alignItems:'center',marginTop:8,padding:12,borderRadius:12,backgroundColor:C.bg,borderWidth:1,borderColor:C.border}}>
      <Text style={{color:C.sub,fontSize:14,width:84}}>{label}</Text>
      <Text selectable style={{color:C.text,fontSize:18,fontWeight:'700',flex:1,letterSpacing:0.5}}>{value}</Text>
      <Text style={{color:C.blue,fontSize:14}}>Copy</Text></TouchableOpacity>);
  const Step=({n,children}:{n:number;children:any})=>(
    <View style={{flexDirection:'row',marginTop:16}}>
      <View style={{width:26,height:26,borderRadius:13,backgroundColor:C.green+'22',alignItems:'center',justifyContent:'center',marginRight:12}}>
        <Text style={{color:C.green,fontWeight:'800',fontSize:13}}>{n}</Text></View>
      <View style={{flex:1}}>{children}</View></View>);
  const P=({children}:{children:any})=><Text style={{color:C.sub,fontSize:15,lineHeight:22}}>{children}</Text>;
  const B=({children}:{children:any})=><Text style={{color:C.text,fontWeight:'700'}}>{children}</Text>;

  // live chain states, all from what the box and cloud actually report
  const sig=relay?.signal_dbm;
  const sigTxt=sig==null?'':sig>=-60?'strong signal':sig>=-70?'good signal':'weak signal: move the box closer to the router';
  const boxOk=!!relay?.box_online||done;
  const camOk=done||relay?.state==='sending'||!!relay?.camera;
  const wifiState:'wait'|'work'|'ok'|'bad'=boxOk?'ok':liveS>=90?'bad':'work';
  const camState:'wait'|'work'|'ok'|'bad'=camOk?'ok':!boxOk?'wait':(relay?.state==='no_camera'&&liveS>=120)?'bad':'work';
  const cloudState:'wait'|'work'|'ok'|'bad'=done?'ok':!camOk?'wait':relay?.state==='no_internet'?'bad':'work';

  const body=()=>{switch(cur){
    case 'plug':return(<>
      <View style={{flexDirection:'row',alignItems:'flex-end',justifyContent:'center',gap:18,marginTop:10,height:190}}>
        <Turntable amount={18}><BoxArt size={150} led="double"/></Turntable>
        <Turntable amount={14}><CamArt size={100}/></Turntable>
      </View>
      <Title t="Let's set up VIGIL" sub="Plug the box in near your Wi-Fi router, and the camera where it will watch."/>
      <View style={[s.card,{marginTop:22}]}>
        <Text style={{color:C.text,fontSize:15,fontWeight:'700'}}>When the box light blinks twice, pauses, blinks twice, like the drawing above, it is ready.</Text>
        <Text style={{color:C.sub,fontSize:14,marginTop:6,lineHeight:20}}>About 2 minutes after plugging it in. Put the camera high in a corner, seeing the floor wall to wall.</Text>
      </View>
    </>);
    case 'tapo':return(<>
      <View style={{alignItems:'center',marginTop:6,height:130,justifyContent:'center'}}><Turntable amount={16}><CamArt size={96}/></Turntable></View>
      <Title t="Wake up the camera" sub="The camera's maker lets only its own app put it on Wi-Fi. About 5 minutes, then you are back here."/>
      <Step n={1}><P>Install <B>Tapo</B> and create a Tapo account.</P>
        <TouchableOpacity onPress={()=>Linking.openURL(TAPO_APP)} style={{marginTop:6}}><Text style={{color:C.blue,fontSize:15,fontWeight:'600'}}>Get Tapo</Text></TouchableOpacity></Step>
      <Step n={2}><P>Tap <B>+</B>, add the camera, and connect it to your home Wi-Fi.</P></Step>
      <Step n={3}><P>Open the camera, tap the gear, then <B>Advanced Settings, Camera Account</B>. Use exactly these:</P>
        <Field label="Username" value="vigil"/>
        <Field label="Password" value={camPass}/></Step>
      <Step n={4}><P>In Tapo: <B>Me, Third-Party Services, Third-Party Compatibility</B>: on.</P></Step>
      <Step n={5}><P>Recommended: camera settings, <B>Firmware Update</B>, automatic updates off.</P></Step>
      <TouchableOpacity onPress={()=>{setTapoDone(!tapoDone);save({tapoDone:!tapoDone});}} style={[s.card,{marginTop:20,flexDirection:'row',alignItems:'center',borderWidth:2,borderColor:tapoDone?C.green:'transparent'}]}>
        <View style={{width:26,height:26,borderRadius:8,borderWidth:2,borderColor:tapoDone?C.green:C.muted,backgroundColor:tapoDone?C.green:'transparent',alignItems:'center',justifyContent:'center'}}>
          {tapoDone&&<Text style={{color:'#000',fontWeight:'900'}}>{'✓'}</Text>}</View>
        <Text style={{color:C.text,fontSize:16,marginLeft:12,flex:1}}>I can see the camera's picture in Tapo, and typed the camera account</Text>
      </TouchableOpacity>
    </>);
    case 'connect':return(<>
      <View style={{alignItems:'center',marginTop:10,height:150,justifyContent:'center'}}><Turntable amount={box?0:20} still={!!box}><BoxArt size={160} led={box?'steady':'double'}/></Turntable></View>
      {!box?(<>
        <Title t="Connect to your VIGIL box" sub={`Your phone joins the box for a moment to give it your Wi-Fi.`}/>
        {!joinFailed&&<TouchableOpacity onPress={joinBox} style={[s.btnPrimary,{marginTop:26,paddingVertical:18}]}>
          <Text style={[s.btnPrimaryTxt,{fontSize:18}]}>{joinTried?'Try again':'Connect'}</Text></TouchableOpacity>}
        {joinTried&&!joinFailed&&<View style={{flexDirection:'row',alignItems:'center',justifyContent:'center',marginTop:16}}>
          <ActivityIndicator color={C.green}/><Text style={{color:C.sub,fontSize:15,marginLeft:10}}>Tap Join when your iPhone asks, then wait a moment.</Text></View>}
        {joinFailed&&(<View style={[s.card,{marginTop:20}]}>
          <Text style={{color:C.text,fontSize:16,fontWeight:'700'}}>Join it in Settings</Text>
          <Step n={1}><P>Open <B>Settings</B>, <B>Wi-Fi</B>, and choose:</P>
            <Field label="Wi-Fi" value={boxSsid}/>
            <Field label="Password" value={boxPw||'(the setup code)'}/></Step>
          <Step n={2}><P>If it says No Internet Connection, that is expected. Come back to VIGIL.</P></Step>
          <View style={{flexDirection:'row',alignItems:'center',marginTop:16}}><ActivityIndicator color={C.green}/><Text style={{color:C.sub,fontSize:14,marginLeft:10}}>Looking for the box...</Text></View>
        </View>)}
        <Text style={{color:C.muted,fontSize:13,marginTop:18,lineHeight:19,textAlign:'center'}}>Box light steady instead of blinking twice? It already has Wi-Fi: tap Back and Continue.</Text>
      </>):(<Animated.View style={{opacity:sheet,transform:[{translateY:sheet.interpolate({inputRange:[0,1],outputRange:[60,0]})}]}}>
        <Title t="VIGIL box found" sub={`${site?.name||cfg.siteId} is ready. Choose your home Wi-Fi, the same one the camera uses.`}/>
        {!!box.last_error&&<Text style={{color:C.orange,fontSize:15,marginTop:12,lineHeight:21,textAlign:'center'}}>{box.last_error}</Text>}
        <View style={[s.card,{marginTop:18,paddingVertical:4}]}>
          {nets.slice(0,10).map(n=>(
            <TouchableOpacity key={n.ssid} onPress={()=>{setSsid(n.ssid);setManual(false);}} style={{flexDirection:'row',alignItems:'center',paddingVertical:13,borderBottomWidth:1,borderBottomColor:C.hair}}>
              <View style={{flexDirection:'row',alignItems:'flex-end',gap:2,marginRight:12}}>
                {[0,1,2,3].map(i=><View key={i} style={{width:4,height:5+i*3,borderRadius:1,backgroundColor:n.signal>=i*25?(ssid===n.ssid&&!manual?C.green:C.sub):C.fill}}/>)}</View>
              <Text style={{color:C.text,fontSize:16,flex:1,fontWeight:ssid===n.ssid&&!manual?'700':'400'}}>{n.ssid}</Text>
              {ssid===n.ssid&&!manual&&<Text style={{color:C.green,fontWeight:'900',fontSize:16}}>{'✓'}</Text>}
            </TouchableOpacity>))}
          <TouchableOpacity onPress={()=>{setManual(true);setSsid('');}} style={{paddingVertical:13}}>
            <Text style={{color:C.blue,fontSize:15}}>{nets.length?'Other network':'Type the Wi-Fi name'}</Text></TouchableOpacity>
        </View>
        {manual&&<TextInput value={ssid} onChangeText={setSsid} placeholder="Wi-Fi name, exactly" placeholderTextColor={C.muted} autoCapitalize="none" autoCorrect={false}
          style={{color:C.text,fontSize:17,padding:14,backgroundColor:C.bg,borderRadius:12,borderWidth:1,borderColor:C.border,marginTop:10}}/>}
        {!!ssid&&<>
          <TextInput value={wpw} onChangeText={setWpw} placeholder={`Password for ${ssid}`} placeholderTextColor={C.muted} secureTextEntry={!showPw}
            autoCapitalize="none" autoCorrect={false} returnKeyType="go" onSubmitEditing={()=>{if(!busy)send();}}
            style={{color:C.text,fontSize:17,padding:14,backgroundColor:C.bg,borderRadius:12,borderWidth:1,borderColor:C.border,marginTop:10}}/>
          <TouchableOpacity onPress={()=>setShowPw(v=>!v)} style={{alignSelf:'flex-end',paddingTop:8}}><Text style={{color:C.sub,fontSize:13}}>{showPw?'Hide password':'Show password'}</Text></TouchableOpacity>
          <TouchableOpacity disabled={busy} onPress={send} style={[s.btnPrimary,{marginTop:14,paddingVertical:18,opacity:busy?0.5:1}]}>
            {busy?<ActivityIndicator color="#000"/>:<Text style={[s.btnPrimaryTxt,{fontSize:18}]}>Connect</Text>}</TouchableOpacity>
        </>}
      </Animated.View>)}
    </>);
    default:return(<>
      {done?(<>
        <View style={{alignItems:'center',marginTop:4}}><Burst/></View>
        <Title t="VIGIL is watching over your home" sub="This is what it sees right now."/>
        <Animated.View style={{opacity:fadeIn,transform:[{scale:fadeIn.interpolate({inputRange:[0,1],outputRange:[0.96,1]})}],
          marginTop:18,borderRadius:16,overflow:'hidden',backgroundColor:'#000',aspectRatio:16/9}}>
          {first?<Image source={{uri:`data:image/jpeg;base64,${first}`}} style={{width:'100%',height:'100%'}} resizeMode="cover"/>
            :<View style={{flex:1,alignItems:'center',justifyContent:'center'}}><ActivityIndicator color={C.green}/></View>}
        </Animated.View>
        <Text style={{color:C.sub,fontSize:14,marginTop:12,textAlign:'center',lineHeight:20}}>Next: tell VIGIL who lives here.</Text>
      </>):(<>
        <View style={{flexDirection:'row',alignItems:'flex-end',justifyContent:'center',gap:18,marginTop:6,height:150}}>
          <Turntable amount={10}><BoxArt size={120} led={boxOk?(camOk?'steady':'slow'):'double'}/></Turntable>
          <Turntable amount={10}><CamArt size={80}/></Turntable>
        </View>
        <Title t="Bringing VIGIL online" sub="Each step lights up as it happens. Your phone goes back to your own Wi-Fi by itself."/>
        <View style={[s.card,{marginTop:20,paddingVertical:6}]}>
          <ChainNode label="Box joins your Wi-Fi" state={wifiState}
            detail={boxOk?[relay?.wifi,sigTxt].filter(Boolean).join(', '):wifiState==='bad'?'Not online yet. The Wi-Fi password may be wrong.':'Joining...'}/>
          <ChainNode label="Box finds the camera" state={camState}
            detail={camOk?'Found it':camState==='bad'?'Cannot find the camera. Check it is on the same Wi-Fi in Tapo, with the camera account exactly as shown.':boxOk?'Looking on your Wi-Fi...':undefined}/>
          <ChainNode label="Video reaches VIGIL" state={cloudState}
            detail={cloudState==='bad'?'Your Wi-Fi has no internet right now.':camOk?'Connecting securely...':undefined}/>
        </View>
        {(wifiState==='bad'||camState==='bad')&&(
          <View style={{marginTop:14}}>
            {wifiState==='bad'&&<TouchableOpacity onPress={()=>{setBox(null);setNets([]);setJoinTried(false);setJoinFailed(false);sheet.setValue(0);go(2);}} style={s.btnPrimary}>
              <Text style={s.btnPrimaryTxt}>Choose the Wi-Fi again</Text></TouchableOpacity>}
            {camState==='bad'&&<TouchableOpacity onPress={()=>go(1)} style={s.btnPrimary}><Text style={s.btnPrimaryTxt}>See the camera steps again</Text></TouchableOpacity>}
          </View>)}
      </>)}
    </>);
  }};
  const canNext=cur==='tapo'?tapoDone:cur==='live'?done:cur!=='connect';
  const doneSteps=['Account','Camera code'];
  return(
    <SafeAreaView style={{flex:1,backgroundColor:C.bg}}>
      <StatusBar barStyle="light-content"/>
      <View style={{paddingHorizontal:20,paddingTop:12}}>
        <View style={{flexDirection:'row',gap:4}}>
          {[...doneSteps,...STEPS].map((_,i)=><View key={i} style={{flex:1,height:4,borderRadius:2,backgroundColor:i<doneSteps.length+step+(done?1:0)?C.green:C.fill}}/>)}
        </View>
      </View>
      <KeyboardAvoidingView style={{flex:1}} behavior={Platform.OS==='ios'?'padding':undefined}>
        <ScrollView contentContainerStyle={{padding:20,paddingBottom:24}} keyboardShouldPersistTaps="handled" automaticallyAdjustKeyboardInsets>
          {body()}
          {!!err&&<Text style={{color:C.red,fontSize:14,marginTop:14,lineHeight:20,textAlign:'center'}}>{err}</Text>}
          {cur!=='live'&&<TouchableOpacity onPress={onSkip} style={{alignItems:'center',paddingTop:24}}>
            <Text style={{color:C.muted,fontSize:14}}>My box and camera are already working</Text></TouchableOpacity>}
        </ScrollView>
        {(cur!=='connect'||!box)&&<View style={{flexDirection:'row',gap:12,paddingHorizontal:20,paddingTop:8,paddingBottom:28}}>
          {step>0&&cur!=='live'&&<TouchableOpacity onPress={()=>go(step-1)} style={{flex:1,backgroundColor:C.fill,borderRadius:14,padding:17,alignItems:'center'}}>
            <Text style={{color:C.text,fontWeight:'700',fontSize:17}}>Back</Text></TouchableOpacity>}
          {cur!=='connect'&&<TouchableOpacity disabled={!canNext} onPress={cur==='live'?onDone:()=>go(step+1)}
            style={[s.btnPrimary,{flex:2,paddingVertical:17},!canNext&&{backgroundColor:C.fill,shadowOpacity:0}]}>
            <Text style={{color:canNext?'#000':C.muted,fontWeight:'700',fontSize:17}}>
              {cur==='plug'?'It is blinking':cur==='live'?(done?'Continue':'Connecting...'):'Continue'}</Text>
          </TouchableOpacity>}
        </View>}
      </KeyboardAvoidingView>
    </SafeAreaView>);
}

// HOME_SETUP_V1: after the camera is added, set up the home properly: everyone
// who lives there (photo, height, who VIGIL looks after), their consent, whose
// Apple Watch this is, who gets alerts, the camera view, then Introduce everyone.
// Each step is saved, so closing the app part-way resumes where it stopped.
type HSPerson={id:string;name:string;resident:boolean;height_cm:number|null;consent:boolean};
const NTFY_APP='https://apps.apple.com/app/ntfy/id1625396347';
function HomeSetup({cfg,role,onDone}:{cfg:CloudCfg;role:string;onDone:()=>void}){
  const owner=role!=='caregiver';
  const STEPS=owner?['people','consent','watch','alerts','camera','meet','done']:['alerts','done'];
  const [step,setStep]=useState(0);
  const [people,setPeople]=useState<HSPerson[]>([]);
  const [units,setUnits]=useState<'us'|'metric'>('us');
  const [form,setForm]=useState<{id?:string;name:string;resident:boolean;height:string;photo:string|null}|null>(null);
  const [busy,setBusy]=useState(false);
  const [err,setErr]=useState<string|null>(null);
  const [watch,setWatch]=useState<string|null|undefined>(undefined);   // undefined = not chosen, null = no watch
  const [frame,setFrame]=useState<string|null>(null);
  const [frameWait,setFrameWait]=useState(0);
  const [code,setCode]=useState('');
  const [meName,setMeName]=useState('');
  const [teach,setTeach]=useState(false);
  const url=cloudSiteUrl(cfg);
  const site=cfg.sites.find(x=>x.id===cfg.siteId);
  const KEY='vigil_home_setup_'+cfg.siteId;
  const cur=STEPS[step];
  useEffect(()=>{applyCloudHeaders(cfg);},[cfg]);
  const load=useCallback(async()=>{
    try{
      const r=await AX.get(`${url}/persons`,{timeout:10000});
      const named=(Array.isArray(r.data)?r.data:[]).filter((p:any)=>(p.name||'').trim());
      setPeople(named.map((p:any)=>({id:p.id,name:p.name,resident:!!p.is_resident,height_cm:p.height_cm??null,consent:!!p.consent_at})));
    }catch(_e){}
  },[url]);
  useEffect(()=>{
    load();
    AsyncStorage.getItem(KEY).then(v=>{const n=parseInt(v||'0',10);if(n>0&&n<STEPS.length)setStep(n);}).catch(()=>{});
    axios.get(`${cfg.gateway}/auth/profile`,{timeout:10000,headers:{'X-VIGIL-Key':cfg.token}}).then(r=>setMeName(r.data?.user?.name||'')).catch(()=>{});
    if(owner)axios.get(`${cfg.gateway}/auth/site/${cfg.siteId}/members`,{timeout:10000,headers:{'X-VIGIL-Key':cfg.token}})
      .then(r=>setCode(r.data?.setup_code||'')).catch(()=>{});
  },[]);
  useEffect(()=>{                                    // live view on the camera step
    if(cur!=='camera')return;
    let alive=true;let n=0;
    (async()=>{while(alive){
      try{const r=await AX.get(`${url}/camera/snapshot?w=800`,{timeout:5000});if(alive&&r.data?.available&&r.data?.frame)setFrame(r.data.frame);}catch(_e){}
      n++;if(alive)setFrameWait(n);
      await new Promise<void>(res=>setTimeout(()=>res(),1000));
    }})();
    return()=>{alive=false;};
  },[cur,url]);
  const go=(n:number)=>{setErr(null);setStep(n);AsyncStorage.setItem(KEY,String(n)).catch(()=>{});};
  const next=()=>go(Math.min(step+1,STEPS.length-1));
  const back=()=>go(Math.max(step-1,0));
  const finish=async()=>{
    await AsyncStorage.removeItem(KEY).catch(()=>{});
    onDone();
  };
  const fmtH=(cm:number|null)=>cm==null?'':units==='us'?`${Math.floor(cm/2.54/12)}' ${Math.round(cm/2.54)%12}"`:`${Math.round(cm)} cm`;
  const pickPhoto=(fromCamera:boolean)=>{
    const opts:any={mediaType:'photo',includeBase64:true,maxWidth:640,maxHeight:640,quality:0.85,selectionLimit:1,cameraType:'front'};
    (fromCamera?launchCamera(opts):launchImageLibrary(opts)).then((r:any)=>{
      if(r?.didCancel)return;
      const b64=r?.assets?.[0]?.base64;
      if(b64&&form)setForm({...form,photo:b64});
      else if(r?.errorCode)setErr(r.errorCode==='camera_unavailable'?'No camera on this device. Choose a photo instead.':'Could not open the photos. Allow VIGIL in Settings, Privacy.');
    }).catch(()=>{});
  };
  const savePerson=async()=>{
    if(!form)return;
    const n=form.name.trim();if(!n){setErr('Type their name.');return;}
    const h=form.height.trim()?parseHeightCm(form.height):null;
    if(form.height.trim()&&h==null){setErr(units==='us'?'Height like 5\'6" or 66 in.':'Height like 168 cm.');return;}
    setBusy(true);setErr(null);
    try{
      let id=form.id;
      if(id){
        await AX.post(`${url}/persons/${id}/identify`,{name:n,is_resident:form.resident},{timeout:10000});
        if(h!=null)await AX.post(`${url}/persons/${id}/height`,{height_cm:h},{timeout:10000});
      }else{
        const r=await AX.post(`${url}/persons/new`,{name:n,is_resident:form.resident,height_cm:h},{timeout:10000});
        id=r.data?.person_id;
      }
      if(id&&form.photo){await AX.post(`${url}/persons/${id}/photo/set`,{photo_b64:form.photo},{timeout:20000});bumpPhoto(id);}
      setForm(null);await load();
    }catch(e:any){setErr(e?.response?.data?.reason||'Could not save. Check the internet connection.');}
    finally{setBusy(false);}
  };
  const removePerson=(p:HSPerson)=>Alert.alert(`Remove ${p.name}?`,'VIGIL forgets them and everything measured about them.',[
    {text:'Cancel',style:'cancel'},
    {text:'Remove',style:'destructive',onPress:async()=>{try{await AX.post(`${url}/persons/${p.id}/delete`,{},{timeout:10000});load();}catch(_e){setErr('Could not remove. Try again.');}}}]);
  const setConsent=async(p:HSPerson,v:boolean)=>{
    setPeople(ps=>ps.map(x=>x.id===p.id?{...x,consent:v}:x));
    try{await AX.post(`${url}/persons/${p.id}/consent`,v?{by:meName||cfg.email}:{given:false},{timeout:10000});}
    catch(_e){setPeople(ps=>ps.map(x=>x.id===p.id?{...x,consent:!v}:x));setErr('Could not save. Check the internet connection.');}
  };
  const saveWatch=async(pid:string|null)=>{
    setWatch(pid);
    const res=people.find(p=>p.resident)??people[0];
    try{
      await AsyncStorage.setItem(SK_WATCH_OWNER,pid??'').catch(()=>{});
      if(!pid)await AsyncStorage.removeItem(SK_WATCH_OWNER).catch(()=>{});
      const profile:SavedProfile={residentName:res?.name??'',residentHeight:String(res?.height_cm??''),emergencyPhone:'',emergencyEmail:'',
        watchEnabled:!!pid,weightKg:'',age:'',units};
      await AsyncStorage.setItem(SK_PROFILE,JSON.stringify(profile));
      AX.post(`${url}/config`,{resident_name:res?.name??'',resident_height_cm:res?.height_cm??170,units,has_apple_watch:pid?1:0},{timeout:10000}).catch(()=>{});
      AX.post(`${url}/config/watch-owner`,{person_id:pid},{timeout:10000}).catch(()=>{});
    }catch(_e){}
  };
  const inp={color:C.text,fontSize:17,paddingHorizontal:14,paddingVertical:14,backgroundColor:C.bg,borderRadius:10,borderWidth:1,borderColor:C.border,marginTop:8};
  const H1=({t,sub}:{t:string;sub?:string})=>(<>
    <Text style={{color:C.text,fontSize:27,fontWeight:'800',letterSpacing:-0.3}}>{t}</Text>
    {!!sub&&<Text style={{color:C.sub,fontSize:16,marginTop:8,lineHeight:23}}>{sub}</Text>}</>);
  const Tick=({on}:{on:boolean})=>(
    <View style={{width:26,height:26,borderRadius:8,borderWidth:2,borderColor:on?C.green:C.muted,backgroundColor:on?C.green:'transparent',alignItems:'center',justifyContent:'center'}}>
      {on&&<Text style={{color:'#000',fontSize:15,fontWeight:'900'}}>{'✓'}</Text>}</View>);
  const residents=people.filter(p=>p.resident);
  const canNext=cur==='people'?(people.length>0&&residents.length>0&&!form)
    :cur==='consent'?people.every(p=>p.consent)
    :cur==='watch'?watch!==undefined:true;
  const body=()=>{switch(cur){
    case 'people':return(<>
      <H1 t="Who lives here?" sub="Add everyone the camera will see most days. VIGIL looks after the people you mark, and learns everyone else so it never mixes them up."/>
      <View style={{flexDirection:'row',backgroundColor:C.card,borderRadius:12,padding:4,marginTop:18,borderWidth:1,borderColor:C.border}}>
        {([['us','Feet and inches'],['metric','Centimetres']] as const).map(([k,l])=>(
          <TouchableOpacity key={k} onPress={()=>setUnits(k)} style={{flex:1,paddingVertical:10,borderRadius:9,alignItems:'center',backgroundColor:units===k?C.green:'transparent'}}>
            <Text style={{color:units===k?'#000':C.sub,fontSize:14,fontWeight:'700'}}>{l}</Text></TouchableOpacity>))}
      </View>
      {people.map(p=>(
        <View key={p.id} style={[s.card,{marginTop:12,flexDirection:'row',alignItems:'center'}]}>
          <PersonAvatar personId={p.id} name={p.name} serverUrl={url} size={52} isResident={p.resident}/>
          <View style={{flex:1,marginLeft:14}}>
            <Text style={{color:C.text,fontSize:17,fontWeight:'700'}}>{p.name}</Text>
            <Text style={{color:p.resident?C.green:C.sub,fontSize:13,marginTop:3}}>{p.resident?'VIGIL looks after them':'Lives here'}{p.height_cm?` · ${fmtH(p.height_cm)}`:''}</Text>
          </View>
          <TouchableOpacity onPress={()=>setForm({id:p.id,name:p.name,resident:p.resident,height:p.height_cm?fmtH(p.height_cm):'',photo:null})} style={{padding:8}}>
            <Text style={{color:C.blue,fontSize:15}}>Edit</Text></TouchableOpacity>
          <TouchableOpacity onPress={()=>removePerson(p)} style={{padding:8}}>
            <Text style={{color:C.muted,fontSize:15}}>Remove</Text></TouchableOpacity>
        </View>))}
      {form?(
        <View style={[s.card,{marginTop:14}]}>
          <Text style={{color:C.text,fontSize:18,fontWeight:'700'}}>{form.id?`Edit ${form.name||'person'}`:'Add a person'}</Text>
          <View style={{flexDirection:'row',alignItems:'center',marginTop:14}}>
            <TouchableOpacity onPress={()=>Alert.alert('Their photo','A clear, front-facing photo helps VIGIL recognise them sooner.',
                [{text:'Take a photo',onPress:()=>pickPhoto(true)},{text:'Choose a photo',onPress:()=>pickPhoto(false)},{text:'Cancel',style:'cancel'}])}
              style={{width:76,height:76,borderRadius:38,backgroundColor:C.fill,alignItems:'center',justifyContent:'center',overflow:'hidden',borderWidth:1,borderColor:C.border}}>
              {form.photo?<Image source={{uri:`data:image/jpeg;base64,${form.photo}`}} style={{width:76,height:76}}/>
                :<Text style={{color:C.green,fontSize:13,fontWeight:'700',textAlign:'center'}}>Add{'\n'}photo</Text>}
            </TouchableOpacity>
            <Text style={{color:C.sub,fontSize:13,lineHeight:19,flex:1,marginLeft:14}}>Optional. A clear photo of their face helps VIGIL recognise them, and shows on their alerts.</Text>
          </View>
          <TextInput style={inp} value={form.name} onChangeText={t=>setForm({...form,name:t})} placeholder="Name, e.g. Nani" placeholderTextColor={C.muted}
            autoCapitalize="words" autoCorrect={false} textContentType="name"/>
          <TextInput style={inp} value={form.height} onChangeText={t=>setForm({...form,height:t})}
            placeholder={units==='us'?'Height, e.g. 5\'4" (optional)':'Height in cm, e.g. 162 (optional)'} placeholderTextColor={C.muted}/>
          <Text style={{color:C.muted,fontSize:12,marginTop:6,lineHeight:17}}>Their real height lets VIGIL measure walking speed accurately.</Text>
          <View style={{flexDirection:'row',alignItems:'center',marginTop:14}}>
            <Switch value={form.resident} onValueChange={v=>setForm({...form,resident:v})} trackColor={{false:C.fill,true:C.green}} thumbColor="#FFF"/>
            <View style={{flex:1,marginLeft:12}}>
              <Text style={{color:C.text,fontSize:15,fontWeight:'600'}}>VIGIL looks after them</Text>
              <Text style={{color:C.sub,fontSize:12,marginTop:2,lineHeight:17}}>Fall alerts, walking and activity trends. Turn off for family who only live there.</Text>
            </View>
          </View>
          <View style={{flexDirection:'row',gap:10,marginTop:16}}>
            <TouchableOpacity onPress={()=>{setForm(null);setErr(null);}} style={{flex:1,padding:15,borderRadius:12,backgroundColor:C.fill,alignItems:'center'}}>
              <Text style={{color:C.text,fontWeight:'700',fontSize:16}}>Cancel</Text></TouchableOpacity>
            <TouchableOpacity disabled={busy||!form.name.trim()} onPress={savePerson} style={[s.btnPrimary,{flex:2,opacity:(busy||!form.name.trim())?0.5:1}]}>
              {busy?<ActivityIndicator color="#000"/>:<Text style={s.btnPrimaryTxt}>Save</Text>}</TouchableOpacity>
          </View>
        </View>
      ):(
        <TouchableOpacity onPress={()=>{setErr(null);setForm({name:'',resident:residents.length===0,height:'',photo:null});}}
          style={{marginTop:14,padding:18,borderRadius:14,borderWidth:1.5,borderStyle:'dashed',borderColor:C.green,alignItems:'center'}}>
          <Text style={{color:C.green,fontSize:17,fontWeight:'700'}}>+ Add {people.length?'another person':'a person'}</Text></TouchableOpacity>
      )}
      {people.length>0&&!residents.length&&!form&&<Text style={{color:C.orange,fontSize:14,marginTop:12}}>Mark at least one person VIGIL looks after.</Text>}
    </>);
    case 'consent':return(<>
      <H1 t="Everyone should know" sub="VIGIL works best, and is fair, when everyone it sees knows about it and agrees."/>
      <View style={[s.card,{marginTop:18}]}>
        {[['No sound','VIGIL never uses a microphone.'],
          ['Video is not recorded','The camera picture is analysed as it arrives and then discarded.'],
          ['What it keeps','Measurements like walking speed, a stick-figure replay around a fall, a small picture of each person, and one still of the room to notice if the camera is moved.'],
          ['Who sees it','Only people you invite to this home in the app.'],
          ['Turn it off any time','Unplug the camera, or remove a person or the whole account in Settings.']].map(([t,d])=>(
          <View key={t} style={{flexDirection:'row',marginBottom:12}}>
            <View style={{width:6,height:6,borderRadius:3,backgroundColor:C.green,marginTop:8,marginRight:12}}/>
            <View style={{flex:1}}><Text style={{color:C.text,fontSize:15,fontWeight:'700'}}>{t}</Text>
              <Text style={{color:C.sub,fontSize:14,marginTop:2,lineHeight:20}}>{d}</Text></View>
          </View>))}
        <Text style={{color:C.blue,fontSize:14}} onPress={()=>Linking.openURL('https://vigilhealth.me/privacy.html')}>Read the privacy policy</Text>
      </View>
      <Text style={[s.secHeader,{marginHorizontal:0,marginTop:20}]}>Tap each person once they have agreed</Text>
      {people.map(p=>(
        <TouchableOpacity key={p.id} onPress={()=>setConsent(p,!p.consent)} style={[s.card,{marginTop:8,flexDirection:'row',alignItems:'center'}]}>
          <Tick on={p.consent}/>
          <Text style={{color:C.text,fontSize:16,marginLeft:14,flex:1}}>{p.name} knows about VIGIL and agrees</Text>
        </TouchableOpacity>))}
      <Text style={{color:C.muted,fontSize:13,marginTop:12,lineHeight:19}}>If someone cannot decide for themselves, the person responsible for their care can agree for them. Visitors should be told the camera is on.</Text>
    </>);
    case 'watch':return(<>
      <H1 t="Apple Watch" sub="Is the Apple Watch paired with this iPhone worn by someone who lives here? Its heart rate, sleep and steps will show only on that person."/>
      <View style={{marginTop:18}}>
        {people.map(p=>(
          <TouchableOpacity key={p.id} onPress={()=>saveWatch(p.id)} style={[s.card,{marginTop:8,flexDirection:'row',alignItems:'center',borderWidth:2,borderColor:watch===p.id?C.green:'transparent'}]}>
            <PersonAvatar personId={p.id} name={p.name} serverUrl={url} size={40}/>
            <Text style={{color:C.text,fontSize:16,fontWeight:'600',marginLeft:12,flex:1}}>{p.name} wears it</Text>
            {watch===p.id&&<Tick on/>}
          </TouchableOpacity>))}
        <TouchableOpacity onPress={()=>saveWatch(null)} style={[s.card,{marginTop:8,flexDirection:'row',alignItems:'center',borderWidth:2,borderColor:watch===null?C.green:'transparent'}]}>
          <Text style={{color:C.text,fontSize:16,fontWeight:'600',flex:1}}>No, nobody here, or no watch</Text>
          {watch===null&&<Tick on/>}
        </TouchableOpacity>
      </View>
      <Text style={{color:C.muted,fontSize:13,marginTop:12,lineHeight:19}}>VIGIL works fully with the camera alone. You can change this in Settings.</Text>
    </>);
    case 'alerts':return(<>
      <H1 t="Who gets alerts" sub={owner?'Falls, a long time on the floor, and the camera going offline. Set up at least one way to hear about them now.':`You joined ${site?.name||'this home'}. Choose how you hear about falls and other alerts.`}/>
      <AccountSection cfg={cfg}/>
      {!!site?.ntfy_topic&&(
        <View style={[s.card,{marginHorizontal:16,marginTop:16}]}>
          <Text style={{color:C.text,fontSize:17,fontWeight:'700'}}>Instant alerts on this phone</Text>
          <Text style={{color:C.sub,fontSize:14,marginTop:6,lineHeight:20}}>1. Install the free ntfy app.{'\n'}2. In ntfy, tap +, paste this topic, and Subscribe:</Text>
          <TouchableOpacity onPress={()=>{Share.share({message:site.ntfy_topic||''});}}
            style={{marginTop:10,padding:12,borderRadius:10,backgroundColor:C.bg,borderWidth:1,borderColor:C.border}}>
            <Text style={{color:C.green,fontSize:15,fontWeight:'700',textAlign:'center'}}>{site.ntfy_topic}</Text>
            <Text style={{color:C.muted,fontSize:12,textAlign:'center',marginTop:4}}>Tap to copy</Text>
          </TouchableOpacity>
          <TouchableOpacity onPress={()=>Linking.openURL(NTFY_APP)} style={{paddingTop:12}}>
            <Text style={{color:C.blue,fontSize:15}}>Get ntfy from the App Store</Text></TouchableOpacity>
          <Text style={{color:C.muted,fontSize:12,marginTop:8,lineHeight:17}}>Keep this topic private: anyone with it receives this home's alerts.</Text>
        </View>)}
      {owner&&!!code&&(
        <View style={[s.card,{marginHorizontal:16,marginTop:16}]}>
          <Text style={{color:C.text,fontSize:17,fontWeight:'700'}}>Invite family</Text>
          <Text style={{color:C.sub,fontSize:14,marginTop:6,lineHeight:20}}>They install VIGIL, create an account, and add the camera with this home's code. You get an email whenever someone joins, and can remove them in Settings.</Text>
          <TouchableOpacity onPress={()=>Share.share({message:`Join me on VIGIL to look after ${site?.name||'our home'}. Install VIGIL, create an account, then tap Add a camera and type this code: ${code}`})}
            style={[s.btnPrimary,{marginTop:12}]}><Text style={s.btnPrimaryTxt}>Send an invite</Text></TouchableOpacity>
        </View>)}
    </>);
    case 'camera':return(<>
      <H1 t="Check the camera view" sub="VIGIL can only help with what it can see."/>
      <View style={{marginTop:16,borderRadius:14,overflow:'hidden',backgroundColor:'#000',aspectRatio:16/9,alignItems:'center',justifyContent:'center'}}>
        {frame?<Image source={{uri:`data:image/jpeg;base64,${frame}`}} style={{width:'100%',height:'100%'}} resizeMode="contain"/>
          :frameWait<15?<ActivityIndicator color={C.green}/>
          :<Text style={{color:C.sub,fontSize:14,textAlign:'center',padding:20,lineHeight:20}}>No picture yet. Check the camera and the VIGIL box are plugged in; the box light should be steady. You can also do this later.</Text>}
      </View>
      <View style={[s.card,{marginTop:16}]}>
        {['Mount it high in a corner, about 2 metres up, tilted down.','The floor where people walk should be visible from wall to wall, and people should be seen head to toe.',
          'Avoid pointing it at a bright window or into a bathroom.','Plug it in where nobody will unplug it, near the VIGIL box.'].map((t,i)=>(
          <View key={i} style={{flexDirection:'row',marginBottom:10}}>
            <Text style={{color:C.green,fontSize:15,fontWeight:'800',width:22}}>{i+1}</Text>
            <Text style={{color:C.sub,fontSize:15,lineHeight:21,flex:1}}>{t}</Text></View>))}
      </View>
    </>);
    case 'meet':return(<>
      <H1 t="Let VIGIL meet everyone" sub={`About a minute per person. Each of ${people.map(p=>p.name).join(', ')||'them'} stands in front of the camera; you tap their name; VIGIL studies their face, build, clothes and walk until a check appears.`}/>
      <TouchableOpacity onPress={()=>setTeach(true)} style={[s.btnPrimary,{marginTop:22,paddingVertical:18}]}>
        <Text style={[s.btnPrimaryTxt,{fontSize:18}]}>Start</Text></TouchableOpacity>
      <Text style={{color:C.muted,fontSize:13,marginTop:12,lineHeight:19}}>Not everyone home? Do it any time: Settings, Camera, Introduce everyone. Until then, VIGIL may say "Not sure" about people.</Text>
    </>);
    default:return(<>
      <View style={{alignItems:'center',marginTop:30}}>
        <View style={{width:96,height:96,borderRadius:48,backgroundColor:C.green,alignItems:'center',justifyContent:'center'}}>
          <Svg width={52} height={52} viewBox="0 0 52 52"><Path d="M12 27 L22 37 L41 16" stroke="#000" strokeWidth={6} fill="none" strokeLinecap="round" strokeLinejoin="round"/></Svg>
        </View>
        <Text style={{color:C.text,fontSize:28,fontWeight:'800',marginTop:20}}>You are all set</Text>
        <Text style={{color:C.sub,fontSize:16,textAlign:'center',lineHeight:23,marginTop:10}}>
          {owner?`VIGIL is watching over ${residents.map(p=>p.name).join(' and ')||'your home'}. Over the first week it learns what is normal for each person, then shows when something changes.`
            :`You will see ${site?.name||'this home'}'s alerts and trends.`}
        </Text>
      </View>
    </>);
  }};
  return(
    <SafeAreaView style={{flex:1,backgroundColor:C.bg}}>
      <StatusBar barStyle="light-content"/>
      <Modal visible={teach} animationType="slide" presentationStyle="fullScreen" onRequestClose={()=>setTeach(false)}>
        {teach&&<TeachWhoScreen serverUrl={url} units={units} onClose={()=>{setTeach(false);next();}}/>}
      </Modal>
      <View style={{paddingHorizontal:20,paddingTop:12}}>
        <View style={{flexDirection:'row',gap:4}}>
          {STEPS.map((_,i)=><View key={i} style={{flex:1,height:4,borderRadius:2,backgroundColor:i<=step?C.green:C.fill}}/>)}
        </View>
      </View>
      <KeyboardAvoidingView style={{flex:1}} behavior={Platform.OS==='ios'?'padding':undefined}>
        <ScrollView contentContainerStyle={{padding:20,paddingBottom:24}} keyboardShouldPersistTaps="handled" automaticallyAdjustKeyboardInsets>
          {body()}
          {!!err&&<Text style={{color:C.red,fontSize:14,marginTop:14,lineHeight:20}}>{err}</Text>}
        </ScrollView>
        <View style={{flexDirection:'row',gap:12,paddingHorizontal:20,paddingTop:8,paddingBottom:28}}>
          {step>0&&cur!=='done'&&<TouchableOpacity onPress={back} style={{flex:1,backgroundColor:C.fill,borderRadius:14,padding:17,alignItems:'center'}}>
            <Text style={{color:C.text,fontWeight:'700',fontSize:17}}>Back</Text></TouchableOpacity>}
          <TouchableOpacity disabled={!canNext} onPress={cur==='done'?finish:next}
            style={[s.btnPrimary,{flex:2,paddingVertical:17},!canNext&&{backgroundColor:C.fill,shadowOpacity:0}]}>
            <Text style={{color:canNext?'#000':C.muted,fontWeight:'700',fontSize:17}}>
              {cur==='done'?'Open VIGIL':cur==='camera'?(frame?'Looks good':'Skip for now'):cur==='meet'?'Later':'Continue'}</Text>
          </TouchableOpacity>
        </View>
      </KeyboardAvoidingView>
    </SafeAreaView>);
}

// MEMBERS_V1 / ACCOUNT_DELETE_V1: who can see this home, inviting family, and
// deleting the account from inside the app (App Store guideline 5.1.1(v)).
function AccessSection({cfg,onDeleted}:{cfg:CloudCfg;onDeleted:()=>void}){
  const [m,setM]=useState<any>(null);
  const [msg,setMsg]=useState<string|null>(null);
  const [del,setDel]=useState(false);
  const [pw,setPw]=useState('');
  const [busy,setBusy]=useState(false);
  const h={timeout:12000,headers:{'X-VIGIL-Key':cfg.token}};
  const site=cfg.sites.find(x=>x.id===cfg.siteId);
  const load=useCallback(()=>{if(!cfg.siteId)return;
    axios.get(`${cfg.gateway}/auth/site/${cfg.siteId}/members`,h).then(r=>setM(r.data)).catch(()=>{});},[cfg.siteId,cfg.token]);
  useEffect(()=>{load();},[load]);
  const remove=(u:any)=>Alert.alert(u.me?`Leave ${site?.name||'this home'}?`:`Remove ${u.name||u.email}?`,
    u.me?'You will stop seeing its camera and alerts.':'They will stop seeing this camera and its alerts.',[
    {text:'Cancel',style:'cancel'},
    {text:u.me?'Leave':'Remove',style:'destructive',onPress:async()=>{
      try{await axios.post(`${cfg.gateway}/auth/site/${cfg.siteId}/members/${u.id}/remove`,{},h);setMsg(u.me?'You left this home. Pull down on Today to refresh.':'Removed.');load();}
      catch(e:any){setMsg(e?.response?.data?.reason||'Could not remove. Check the internet connection.');}}}]);
  const makeOwner=(u:any)=>Alert.alert(`Make ${u.name||u.email} an owner?`,'Owners can invite and remove people.',[
    {text:'Cancel',style:'cancel'},
    {text:'Make owner',onPress:async()=>{try{await axios.post(`${cfg.gateway}/auth/site/${cfg.siteId}/members/${u.id}/owner`,{},h);load();}
      catch(e:any){setMsg(e?.response?.data?.reason||'Could not change. Check the internet connection.');}}}]);
  const deleteAccount=async()=>{
    setBusy(true);setMsg(null);
    try{await axios.post(`${cfg.gateway}/auth/delete`,{password:pw},h);setPw('');setDel(false);
      Alert.alert('Account deleted','Your VIGIL account and sign-in details are deleted. The camera keeps working for the rest of the family.');onDeleted();}
    catch(e:any){setMsg(e?.response?.data?.reason||'Could not delete. Check the internet connection.');}
    finally{setBusy(false);}
  };
  const inp={color:C.text,fontSize:16,paddingHorizontal:12,paddingVertical:12,backgroundColor:C.bg,borderRadius:10,borderWidth:1,borderColor:C.border,marginTop:8};
  return(
    <View style={[s.card,{marginHorizontal:16,marginTop:16}]}>
      <Text style={{color:C.text,fontSize:17,fontWeight:'700'}}>People with access{site?` to ${site.name}`:''}</Text>
      <Text style={{color:C.sub,fontSize:13,marginTop:4,lineHeight:18}}>Everyone here can see the camera, trends and alerts.</Text>
      {(m?.members||[]).map((u:any)=>(
        <View key={u.id} style={{flexDirection:'row',alignItems:'center',paddingVertical:11,borderTopWidth:1,borderTopColor:C.hair,marginTop:6}}>
          <View style={{flex:1}}>
            <Text style={{color:C.text,fontSize:15,fontWeight:'600'}}>{u.name||u.email}{u.me?' (you)':''}</Text>
            <Text style={{color:C.muted,fontSize:12,marginTop:2}}>{u.email} · {u.role==='owner'?'Owner':'Family'}</Text>
          </View>
          {m?.my_role==='owner'&&!u.me&&u.role!=='owner'&&<TouchableOpacity onPress={()=>makeOwner(u)} style={{padding:6}}>
            <Text style={{color:C.blue,fontSize:14}}>Owner</Text></TouchableOpacity>}
          {(u.me||(m?.my_role==='owner'&&u.role!=='owner'))&&<TouchableOpacity onPress={()=>remove(u)} style={{padding:6}}>
            <Text style={{color:C.red,fontSize:14}}>{u.me?'Leave':'Remove'}</Text></TouchableOpacity>}
        </View>))}
      {m?.my_role==='owner'&&!!m?.setup_code&&(
        <TouchableOpacity onPress={()=>Share.share({message:`Join me on VIGIL to look after ${site?.name||'our home'}. Install VIGIL, create an account, then tap Add a camera and type this code: ${m.setup_code}`})}
          style={[s.btnPrimary,{marginTop:12}]}><Text style={s.btnPrimaryTxt}>Invite family</Text></TouchableOpacity>)}
      <View style={{marginTop:18,paddingTop:14,borderTopWidth:1,borderTopColor:C.hair}}>
        {!del?(
          <TouchableOpacity onPress={()=>{setDel(true);setMsg(null);}}><Text style={{color:C.red,fontSize:15}}>Delete my account</Text></TouchableOpacity>
        ):(<>
          <Text style={{color:C.text,fontSize:15,fontWeight:'700'}}>Delete your VIGIL account?</Text>
          <Text style={{color:C.sub,fontSize:13,marginTop:4,lineHeight:18}}>This deletes your sign-in, name, email, phone and access, right away. It cannot be undone. The camera and the people it knows stay with the home for the rest of the family{m?.my_role==='owner'?'; another member becomes owner':''}.</Text>
          <TextInput style={inp} value={pw} onChangeText={setPw} placeholder="Your password" placeholderTextColor={C.muted} secureTextEntry autoCapitalize="none" textContentType="password"/>
          <View style={{flexDirection:'row',gap:10,marginTop:10}}>
            <TouchableOpacity onPress={()=>{setDel(false);setPw('');}} style={{flex:1,padding:13,borderRadius:10,backgroundColor:C.fill,alignItems:'center'}}>
              <Text style={{color:C.text,fontWeight:'700'}}>Cancel</Text></TouchableOpacity>
            <TouchableOpacity disabled={busy||!pw} onPress={deleteAccount} style={{flex:1,padding:13,borderRadius:10,backgroundColor:C.red,alignItems:'center',opacity:(busy||!pw)?0.5:1}}>
              {busy?<ActivityIndicator color="#fff"/>:<Text style={{color:'#fff',fontWeight:'700'}}>Delete</Text>}</TouchableOpacity>
          </View>
        </>)}
      </View>
      {msg&&<Text style={{color:C.sub,fontSize:13,marginTop:10,lineHeight:18}}>{msg}</Text>}
    </View>);
}

// Module level so its identity is stable: defined inside OnboardingScreen it was
// recreated on every keystroke, which remounted the TextInput and closed the keyboard.
const OBField = ({label,value,onChange,placeholder,keyboard='default',note}:any) => (
  <View style={{marginBottom:20}}>
    <Text style={{color:C.sub,fontSize:14,fontWeight:'600',marginBottom:8}}>{label}</Text>
    {note && <Text style={{color:C.muted,fontSize:12,marginBottom:8,lineHeight:17}}>{note}</Text>}
    <View style={{backgroundColor:C.card,borderRadius:12,borderWidth:1,borderColor:C.border}}>
      <TextInput
        style={{color:C.text,fontSize:18,paddingHorizontal:16,paddingVertical:16}}
        value={value} onChangeText={onChange} placeholder={placeholder}
        placeholderTextColor={C.muted} keyboardType={keyboard}
        autoCapitalize="none" autoCorrect={false}/>
    </View>
  </View>
);

function OnboardingScreen({onComplete,onDemo,onCloud,pendingCode}:OBProps&{onCloud?:(cfg:CloudCfg)=>void;pendingCode?:string|null}) {
  // SIMPLE_SETUP_V1: most people only sign in. The local monitoring-unit
  // setup is still here, one tap away, and down to three short screens.
  const [mode,setMode]             = useState<'choose'|'cloud'|'local'>(pendingCode?'cloud':'choose');
  useEffect(()=>{if(pendingCode)setMode(m=>m==='choose'?'cloud':m);},[pendingCode]);   // APP_SETUP_V1: opened from the card's QR code
  const [step,setStep]             = useState(1);
  const [residentName,setRN]       = useState('');
  const [dob,setDOB]               = useState('');
  const [residentHeight,setRH]     = useState('170');
  const [emergencyPhone,setEP]     = useState('');
  const [emergencyEmail,setEE]     = useState('');
  const [watchEnabled,setWE]       = useState(false);
  // PROFILE_V2: basic measurements, in the units the family uses.
  const [units,setUnits]           = useState<'us'|'metric'>('us');
  const [heightFt,setHFt]          = useState('');
  const [heightIn,setHIn]          = useState('');
  const [weight,setWeight]         = useState('');
  const [age,setAge]               = useState('');
  const [cloudCfg,setCloudCfg]     = useState<CloudCfg|null>(null);
  const [pendingCloud,setPendingCloud] = useState<CloudCfg|null>(null);   // ACCOUNTS_V1: signed in, no camera yet
  const [homeSetup,setHomeSetup]   = useState<{cfg:CloudCfg;role:string}|null>(null);   // HOME_SETUP_V1
  const [boxSetup,setBoxSetup]     = useState<CloudCfg|null>(null);                    // APP_SETUP_V1
  const [serverIp,setSI]           = useState(DEFAULT_IP);
  const [serverPort,setSP]         = useState(DEFAULT_PORT);
  const [testing,setTesting]       = useState(false);
  const [serverOk,setServerOk]     = useState<boolean|null>(null);
  const [agreed,setAgreed]         = useState(false);
  const TOTAL = 6;
  // PROFILE_V2: who + measurements, Apple Watch, then connect (local) or done (cloud).
  const LOCAL_STEPS = cloudCfg ? [1,4,5] : [1,4,3,5];
  const heightCm = ():number|null=>{
    if(units==='metric'){const v=parseFloat(residentHeight);return v>=90&&v<=230?v:null;}
    const ft=parseFloat(heightFt)||0, inch=parseFloat(heightIn)||0;
    const cm=(ft*12+inch)*2.54; return cm>=90&&cm<=230?Math.round(cm):null;
  };
  const weightKg = ():number|null=>{
    const v=parseFloat(weight); if(!v)return null;
    const kg=units==='metric'?v:v*0.45359237; return kg>=20&&kg<=250?Math.round(kg*10)/10:null;
  };

  const testConnection = async () => {
    setTesting(true); setServerOk(null);
    try {
      const r = await axios.get(`http://${serverIp}:${serverPort}/health`,{timeout:6000});
      setServerOk(r.data?.status==='ok');
    } catch { setServerOk(false); }
    setTesting(false);
  };

  const canNext = () => {
    if(step===0)return agreed;
    if(step===3)return serverOk===true;
    if(step===1){return residentName.trim().length>0 && heightCm()!==null;}
    if(step===2)return emergencyPhone.trim().length>=10;
    return true;
  };


  const renderStep = () => {
    switch(step) {
      case 0: return (
        <View style={{flex:1}}>
          <View style={{alignItems:'center',marginTop:20,marginBottom:32}}>
            <View style={{width:80,height:80,borderRadius:20,backgroundColor:C.green+'22',alignItems:'center',justifyContent:'center',marginBottom:16}}>
              <Text style={{fontSize:36}}>◎</Text>
            </View>
            <Text style={{color:C.text,fontSize:28,fontWeight:'800',letterSpacing:-0.5,marginBottom:8}}>Welcome to VIGIL</Text>
            <Text style={{color:C.sub,fontSize:15,textAlign:'center',lineHeight:22,paddingHorizontal:8}}>
              Passive health monitoring for retirement homes. VIGIL watches for changes in activity, gait, and vital signs — alerting caregivers when something seems off.
            </Text>
          </View>
          <View style={{backgroundColor:C.card,borderRadius:16,padding:20,marginBottom:20}}>
            <Text style={{color:C.text,fontSize:15,fontWeight:'700',marginBottom:12}}>Before you begin</Text>
            {[
              ['Camera required','A Tapo or Reolink camera connected to the same Wi-Fi as the VIGIL server.'],
              ['VIGIL monitoring unit','The small computer that analyzes the camera feed inside the room.'],
              ['Optional: Apple Watch','If the resident has an Apple Watch, VIGIL can sync health metrics automatically.'],
            ].map(([title,desc])=>(
              <View key={title} style={{flexDirection:'row',gap:12,marginBottom:12}}>
                <View style={{width:6,height:6,borderRadius:3,backgroundColor:C.green,marginTop:6}}/>
                <View style={{flex:1}}>
                  <Text style={{color:C.text,fontSize:14,fontWeight:'600'}}>{title}</Text>
                  <Text style={{color:C.sub,fontSize:13,lineHeight:19,marginTop:2}}>{desc}</Text>
                </View>
              </View>
            ))}
          </View>
          <TouchableOpacity style={{flexDirection:'row',alignItems:'flex-start',gap:12,marginBottom:20}} onPress={()=>setAgreed(p=>!p)}>
            <View style={{width:22,height:22,borderRadius:6,borderWidth:2,borderColor:agreed?C.green:C.muted,backgroundColor:agreed?C.green:'transparent',alignItems:'center',justifyContent:'center',marginTop:1}}>
              {agreed && <Text style={{color:'#000',fontSize:13,fontWeight:'800'}}>✓</Text>}
            </View>
            <Text style={{color:C.sub,fontSize:13,lineHeight:19,flex:1}}>
              I understand VIGIL is a wellness monitoring tool and not a medical device. I have read and agree to the Terms of Service and Privacy Policy.
            </Text>
          </TouchableOpacity>
        </View>
      );
      case 1: return (
        <View style={{flex:1}}>
          <Text style={{color:C.text,fontSize:26,fontWeight:'800',marginBottom:8}}>Who is VIGIL watching over?</Text>
          <Text style={{color:C.sub,fontSize:16,marginBottom:28,lineHeight:22}}>
            Their height lets VIGIL measure their walking speed. Everyone else in the home is added after setup.
          </Text>
          <OBField label="First name" value={residentName} onChange={setRN} placeholder="e.g. Margaret"/>
          <View style={{flexDirection:'row',backgroundColor:C.card,borderRadius:12,padding:4,marginBottom:20,borderWidth:1,borderColor:C.border}}>
            {([['us','Feet and inches'],['metric','Centimetres']] as const).map(([k,lbl])=>(
              <TouchableOpacity key={k} onPress={()=>setUnits(k)} style={{flex:1,paddingVertical:12,borderRadius:9,alignItems:'center',backgroundColor:units===k?C.green:'transparent'}}>
                <Text style={{color:units===k?'#000':C.sub,fontSize:14,fontWeight:'700'}}>{lbl}</Text>
              </TouchableOpacity>
            ))}
          </View>
          {units==='us'?(
            <View style={{flexDirection:'row',gap:12}}>
              <View style={{flex:1}}><OBField label="Height (feet)" value={heightFt} onChange={setHFt} placeholder="5" keyboard="number-pad"/></View>
              <View style={{flex:1}}><OBField label="and inches" value={heightIn} onChange={setHIn} placeholder="6" keyboard="number-pad"/></View>
            </View>
          ):(
            <OBField label="Height (cm)" value={residentHeight} onChange={setRH} placeholder="170" keyboard="number-pad"/>
          )}
        </View>
      );
      case 2: return (
        <View style={{flex:1}}>
          <Text style={{color:C.text,fontSize:24,fontWeight:'800',marginBottom:6}}>Emergency Contact</Text>
          <Text style={{color:C.sub,fontSize:14,marginBottom:28,lineHeight:20}}>
            This person receives an SMS immediately when a fall or extended period of inactivity is detected.
          </Text>
          <View style={{backgroundColor:C.card,borderRadius:12,borderWidth:1,borderColor:C.border,padding:14,marginBottom:24}}>
            <Text style={{color:C.text,fontSize:13,fontWeight:'600',marginBottom:4}}>Text message alerts</Text>
            <Text style={{color:C.sub,fontSize:12,lineHeight:18}}>
              Enter the mobile number that should receive fall and inactivity alerts for this resident. Message and data rates may apply. Reply STOP at any time to stop receiving messages, or HELP for help. This number is never shared for marketing.
            </Text>
            <View style={{flexDirection:'row',gap:16,marginTop:10}}>
              <Text style={{color:C.blue,fontSize:12,fontWeight:'600'}}
                onPress={()=>Linking.openURL('https://vigilhealth.me/privacy.html')}>Privacy Policy</Text>
              <Text style={{color:C.blue,fontSize:12,fontWeight:'600'}}
                onPress={()=>Linking.openURL('https://vigilhealth.me/terms.html')}>Terms</Text>
            </View>
          </View>
          <OBField label="Phone Number" value={emergencyPhone} onChange={setEP} placeholder="+1 (206) 555-0000" keyboard="phone-pad"
            note="Must include country code. e.g. +12065550000"/>
          <OBField label="Email (optional)" value={emergencyEmail} onChange={setEE} placeholder="family@example.com" keyboard="email-address"/>
        </View>
      );
      case 3: return (
        <View style={{flex:1}}>
          <Text style={{color:C.text,fontSize:26,fontWeight:'800',marginBottom:8}}>Connect to your VIGIL box</Text>
          <Text style={{color:C.sub,fontSize:16,marginBottom:28,lineHeight:22}}>
            Type the address from the card that came with the box, then tap Test.
          </Text>
          <OBField label="Unit Address" value={serverIp} onChange={setSI} placeholder="e.g. 192.168.1.50"
            keyboard="numbers-and-punctuation"
            note="Shown on the setup card that came with your unit."/>
          {!!onDemo && (
            <TouchableOpacity onPress={onDemo} style={{alignItems:'center',paddingVertical:8,marginBottom:12}}>
              <Text style={{color:C.blue,fontSize:13,fontWeight:'600'}}>No unit yet? Explore VIGIL in Demo Mode</Text>
            </TouchableOpacity>
          )}
          <TouchableOpacity
            style={{backgroundColor:testing?C.fill:C.blue,borderRadius:12,padding:14,alignItems:'center',marginBottom:16,opacity:testing?0.7:1}}
            onPress={testConnection} disabled={testing}>
            {testing
              ? <View style={{flexDirection:'row',gap:8,alignItems:'center'}}><ActivityIndicator color="#fff" size="small"/><Text style={{color:'#fff',fontWeight:'700'}}>Testing...</Text></View>
              : <Text style={{color:'#fff',fontSize:17,fontWeight:'700'}}>Test</Text>}
          </TouchableOpacity>
          {serverOk===true && (
            <View style={{backgroundColor:C.green+'18',borderRadius:12,padding:12,alignItems:'center'}}>
              <Text style={{color:C.green,fontWeight:'700'}}>✓  Connected successfully</Text>
            </View>
          )}
          {serverOk===false && (
            <View style={{backgroundColor:C.red+'18',borderRadius:12,padding:12}}>
              <Text style={{color:C.red,fontWeight:'700',marginBottom:4}}>✕  Connection failed</Text>
              <Text style={{color:C.red,fontSize:12,lineHeight:18}}>
                Check that the monitoring unit is powered on and that your phone is on the same network as the unit, then try again.
              </Text>
            </View>
          )}
          {serverOk===false && !!onDemo && (
            <View style={{marginTop:14,backgroundColor:C.card,borderRadius:12,
                          padding:14,borderWidth:1,borderColor:C.border}}>
              <Text style={{color:C.sub,fontSize:12,lineHeight:18,marginBottom:12}}>
                No monitoring unit available? Demo Mode shows every feature
                using sample data, with no hardware required.
              </Text>
              <TouchableOpacity onPress={onDemo}
                style={{backgroundColor:C.green,borderRadius:12,paddingVertical:12,
                        alignItems:'center'}}>
                <Text style={{color:'#000',fontWeight:'800',fontSize:15}}>Open Demo Mode</Text>
              </TouchableOpacity>
            </View>
          )}
        </View>
      );
      case 4: return (
        <View style={{flex:1}}>
          <Text style={{color:C.text,fontSize:26,fontWeight:'800',marginBottom:8}}>Does {residentName.trim()||'they'} wear an Apple Watch?</Text>
          <Text style={{color:C.sub,fontSize:16,marginBottom:28,lineHeight:22}}>
            VIGIL works fully with the camera alone. If the watch is paired with this iPhone, VIGIL can also show their heart rate, sleep and steps.
          </Text>
          <TouchableOpacity
            style={{backgroundColor:C.card,borderRadius:16,padding:20,marginBottom:12,borderWidth:2,borderColor:watchEnabled?C.green:C.border}}
            onPress={()=>setWE(true)}>
            <View style={{flexDirection:'row',alignItems:'center',gap:14}}>
              <View style={{width:44,height:44,borderRadius:12,backgroundColor:C.green+'22',alignItems:'center',justifyContent:'center'}}>
                <Text style={{fontSize:22}}>⌚</Text>
              </View>
              <View style={{flex:1}}>
                <Text style={{color:C.text,fontSize:15,fontWeight:'700'}}>Yes — connect Apple Watch</Text>
                <Text style={{color:C.sub,fontSize:13,marginTop:3}}>Adds HRV, heart rate, sleep, and SpO2 data</Text>
              </View>
              {watchEnabled && <View style={{width:20,height:20,borderRadius:10,backgroundColor:C.green,alignItems:'center',justifyContent:'center'}}><Text style={{color:'#000',fontSize:12,fontWeight:'800'}}>✓</Text></View>}
            </View>
          </TouchableOpacity>
          <TouchableOpacity
            style={{backgroundColor:C.card,borderRadius:16,padding:20,borderWidth:2,borderColor:!watchEnabled?C.green:C.border}}
            onPress={()=>setWE(false)}>
            <View style={{flexDirection:'row',alignItems:'center',gap:14}}>
              <View style={{width:44,height:44,borderRadius:12,backgroundColor:C.fill,alignItems:'center',justifyContent:'center'}}>
                <Text style={{fontSize:22}}>◎</Text>
              </View>
              <View style={{flex:1}}>
                <Text style={{color:C.text,fontSize:15,fontWeight:'700'}}>No — camera only</Text>
                <Text style={{color:C.sub,fontSize:13,marginTop:3}}>VIGIL monitors activity, gait, and detects falls automatically</Text>
              </View>
              {!watchEnabled && <View style={{width:20,height:20,borderRadius:10,backgroundColor:C.green,alignItems:'center',justifyContent:'center'}}><Text style={{color:'#000',fontSize:12,fontWeight:'800'}}>✓</Text></View>}
            </View>
          </TouchableOpacity>
        </View>
      );
      case 5: return (
        <View style={{flex:1}}>
          <View style={{alignItems:'center',marginTop:20,marginBottom:32}}>
            <View style={{width:80,height:80,borderRadius:40,backgroundColor:C.green+'22',alignItems:'center',justifyContent:'center',marginBottom:16}}>
              <Text style={{fontSize:36}}>◎</Text>
            </View>
            <Text style={{color:C.text,fontSize:28,fontWeight:'800',letterSpacing:-0.5,marginBottom:8}}>All Set</Text>
            <Text style={{color:C.sub,fontSize:15,textAlign:'center',lineHeight:22}}>
              VIGIL is ready to monitor {residentName||'the resident'}. The camera will begin learning their normal activity pattern immediately.
            </Text>
          </View>
          {[
            ['Watching over',residentName||'—'],
            ['Apple Watch',watchEnabled?'Yes':'No'],
          ].map(([label,val])=>(
            <View key={label} style={{flexDirection:'row',justifyContent:'space-between',paddingVertical:12,borderBottomWidth:1,borderBottomColor:C.border}}>
              <Text style={{color:C.sub,fontSize:14}}>{label}</Text>
              <Text style={{color:C.text,fontSize:14,fontWeight:'500'}}>{val}</Text>
            </View>
          ))}
          <View style={{marginTop:24,backgroundColor:C.card,borderRadius:14,padding:16}}>
            <Text style={{color:C.sub,fontSize:13,lineHeight:19,textAlign:'center'}}>
              You can update any of these settings at any time from the Settings tab. VIGIL learns the resident's baseline over the first 3 days of monitoring.
            </Text>
          </View>
        </View>
      );
      default: return null;
    }
  };

  const stepTitles = ['Welcome','Who is VIGIL watching over?','Emergency','Connect','Watch','Ready'];
  const li = LOCAL_STEPS.indexOf(step);

  const finish = async () => {
    const hc=heightCm(); const wk=weightKg();
    const profile:SavedProfile = {residentName:residentName.trim(),residentHeight:String(hc??residentHeight),
      emergencyPhone,emergencyEmail,watchEnabled,weightKg:wk!=null?String(wk):'',age,units};
    await AsyncStorage.setItem(SK_PROFILE,JSON.stringify(profile));
    await AsyncStorage.setItem(SK_ONBOARDING,'done');
    const body={resident_name:profile.residentName,resident_height_cm:hc??170,resident_weight_kg:wk,
      resident_age:parseInt(age,10)||null,units,has_apple_watch:watchEnabled?1:0,
      emergency_phone:emergencyPhone||null,emergency_email:emergencyEmail||null};
    if(cloudCfg){
      applyCloudHeaders(cloudCfg);
      AX.post(`${cloudSiteUrl(cloudCfg)}/config`,body,{timeout:8000}).catch(()=>{});
      onCloud&&onCloud(cloudCfg);
      return;
    }
    await AsyncStorage.setItem(SK_SERVER,JSON.stringify({ip:serverIp,port:serverPort}));
    AX.post(`http://${serverIp}:${serverPort}/config`,body,{timeout:8000}).catch(()=>{});
    onComplete(profile,serverIp,serverPort);
  };

  const legal=(
    <Text style={{color:C.muted,fontSize:13,lineHeight:19,textAlign:'center',marginTop:18}}>
      VIGIL is a safety and wellness tool, not a medical device. By continuing you agree to the{' '}
      <Text style={{color:C.blue}} onPress={()=>Linking.openURL('https://vigilhealth.me/terms.html')}>Terms</Text> and{' '}
      <Text style={{color:C.blue}} onPress={()=>Linking.openURL('https://vigilhealth.me/privacy.html')}>Privacy Policy</Text>.
    </Text>
  );

  if(boxSetup){
    return <BoxSetup cfg={boxSetup} onDone={()=>setBoxSetup(null)} onSkip={()=>setBoxSetup(null)}/>;
  }
  if(homeSetup){
    return <HomeSetup cfg={homeSetup.cfg} role={homeSetup.role} onDone={()=>{
      AsyncStorage.removeItem('vigil_home_setup_role').catch(()=>{});onCloud&&onCloud(homeSetup.cfg);}}/>;
  }
  if(mode!=='local'){
    return (
      <SafeAreaView style={{flex:1,backgroundColor:C.bg}}>
        <StatusBar barStyle="light-content"/>
        <KeyboardAvoidingView style={{flex:1}} behavior={Platform.OS==='ios'?'padding':undefined}>
          <ScrollView contentContainerStyle={{padding:24,paddingTop:48}} keyboardShouldPersistTaps="handled">
            <View style={{alignItems:'center',marginBottom:28}}>
              <Image source={{uri:'LaunchLogo'}} style={{width:84,height:84,borderRadius:20,marginBottom:18}}/>
              <Text style={{color:C.text,fontSize:32,fontWeight:'800',letterSpacing:-0.5}}>VIGIL</Text>
              <Text style={{color:C.sub,fontSize:17,textAlign:'center',lineHeight:24,marginTop:8}}>
                Tells you right away if someone falls. Nothing to wear, nothing to press.
              </Text>
            </View>
            {mode==='cloud'&&!!pendingCode&&!pendingCloud&&(
              <View style={{marginHorizontal:16,padding:14,borderRadius:14,backgroundColor:C.green+'18',borderWidth:1,borderColor:C.green+'55'}}>
                <Text style={{color:C.text,fontSize:15,fontWeight:'700'}}>Setting up your VIGIL camera</Text>
                <Text style={{color:C.sub,fontSize:14,marginTop:4,lineHeight:20}}>Code {pendingCode.slice(0,4)}-{pendingCode.slice(4)} is ready. Create your account and VIGIL adds the camera for you.</Text>
              </View>)}
            {mode==='cloud'?(
              <>
                {pendingCloud?(
                  <AddCameraCard cfg={pendingCloud} initialCode={pendingCode||''}
                    onAdded={(cfg,role)=>{setPendingCloud(null);
                      // HOME_SETUP_V1: the first person sets up the whole home; family who join later choose their alerts.
                      AsyncStorage.setItem('vigil_home_setup_role',JSON.stringify({siteId:cfg.siteId,role:role||'owner'})).catch(()=>{});
                      if((role||'owner')==='owner'&&!cfg.sites.find(x=>x.id===cfg.siteId)?.online)setBoxSetup(cfg);   // camera not sending yet: set up the box
                      setHomeSetup({cfg,role:role||'owner'});}}
                    onSkip={()=>{const c=pendingCloud;setPendingCloud(null);onCloud&&onCloud(c);}}/>
                ):(
                  <CloudSignIn onSignedIn={async cfg=>{
                    if(!cfg.sites.length){setPendingCloud(cfg);return;}
                    // HOME_SETUP_V1: setup left part-way on this phone resumes; otherwise straight in.
                    try{const v=JSON.parse((await AsyncStorage.getItem('vigil_home_setup_role'))||'null');
                      if(v&&cfg.sites.some(x=>x.id===v.siteId)){const c2={...cfg,siteId:v.siteId};
                        if(v.role==='owner'&&(await AsyncStorage.getItem('vigil_box_setup_'+v.siteId)))setBoxSetup(c2);
                        setHomeSetup({cfg:c2,role:v.role});return;}}catch(_e){}
                    onCloud&&onCloud(cfg);}}/>
                )}
                <TouchableOpacity onPress={()=>setMode('choose')} style={{alignItems:'center',paddingVertical:16}}>
                  <Text style={{color:C.sub,fontSize:16}}>Back</Text>
                </TouchableOpacity>
              </>
            ):(
              <>
                <TouchableOpacity style={[s.btnPrimary,{paddingVertical:18}]} onPress={()=>setMode('cloud')}>
                  <Text style={[s.btnPrimaryTxt,{fontSize:18}]}>Get started</Text>
                </TouchableOpacity>
                <TouchableOpacity onPress={()=>{setMode('local');setStep(1);}}
                  style={{marginTop:14,paddingVertical:16,borderRadius:14,borderWidth:1,borderColor:C.border,alignItems:'center'}}>
                  <Text style={{color:C.text,fontSize:16,fontWeight:'600'}}>I have a VIGIL box at home</Text>
                </TouchableOpacity>
                {!!onDemo&&(
                  <TouchableOpacity onPress={onDemo} style={{alignItems:'center',paddingVertical:18}}>
                    <Text style={{color:C.blue,fontSize:16}}>Just looking? Try the demo</Text>
                  </TouchableOpacity>
                )}
              </>
            )}
            {legal}
          </ScrollView>
        </KeyboardAvoidingView>
      </SafeAreaView>
    );
  }

  const goNext=()=>{ if(li<LOCAL_STEPS.length-1)setStep(LOCAL_STEPS[li+1]); else finish(); };
  const goBack=()=>{ if(li>0)setStep(LOCAL_STEPS[li-1]); else {setCloudCfg(null);setMode('choose');} };
  return (
    <SafeAreaView style={{flex:1,backgroundColor:C.bg}}>
      <StatusBar barStyle="light-content"/>
      <View style={{paddingHorizontal:20,paddingTop:12,paddingBottom:4}}>
        <View style={{flexDirection:'row',gap:4,marginBottom:6}}>
          {LOCAL_STEPS.map((_,i)=>(
            <View key={i} style={{flex:1,height:4,borderRadius:2,backgroundColor:i<=li?C.green:C.fill}}/>
          ))}
        </View>
        <Text style={{color:C.muted,fontSize:13,textAlign:'right'}}>Step {li+1} of {LOCAL_STEPS.length}</Text>
      </View>

      <KeyboardAvoidingView style={{flex:1}} behavior={Platform.OS==='ios'?'padding':undefined}>
        <ScrollView contentContainerStyle={{padding:24,paddingBottom:12}} keyboardShouldPersistTaps="handled" showsVerticalScrollIndicator={false}>
          {renderStep()}
        </ScrollView>
        <View style={{flexDirection:'row',gap:12,padding:20,paddingBottom:32}}>
          <TouchableOpacity style={{flex:1,backgroundColor:C.fill,borderRadius:14,padding:18,alignItems:'center'}} onPress={goBack}>
            <Text style={{color:C.text,fontWeight:'700',fontSize:17}}>Back</Text>
          </TouchableOpacity>
          <TouchableOpacity
            style={[s.btnPrimary,{flex:2,paddingVertical:18},!canNext()&&{backgroundColor:C.fill,shadowOpacity:0}]}
            onPress={goNext}
            disabled={!canNext()}>
            <Text style={{color:canNext()?'#000':C.muted,fontWeight:'700',fontSize:17}}>
              {li===LOCAL_STEPS.length-1?'Start':'Continue'}
            </Text>
          </TouchableOpacity>
        </View>
      </KeyboardAvoidingView>
    </SafeAreaView>
  );
}

// ─────────────────────────────────────────────
// ACTIVITY RING
// ─────────────────────────────────────────────
function ActivityRing({pct,activeMin,stillMin}:{pct:number|null;activeMin:number|null;stillMin:number|null}) {
  const fraction = Math.min(1,Math.max(0,(pct??0)/100));
  const dash = fraction * CIRC;
  const cx = RING/2;
  return (
    <View style={{alignItems:'center',paddingVertical:20}}>
      <View style={{width:RING,height:RING,justifyContent:'center',alignItems:'center'}}>
        <Svg width={RING} height={RING} style={{position:'absolute'}}>
          <Circle cx={cx} cy={cx} r={RADIUS} stroke={C.green+'28'} strokeWidth={STROKE} fill="none"/>
          {fraction>0 && (
            <Circle cx={cx} cy={cx} r={RADIUS} stroke={C.green} strokeWidth={STROKE} fill="none"
              strokeDasharray={`${dash} ${CIRC-dash}`} strokeLinecap="round"
              rotation={-90} origin={`${cx},${cx}`}/>
          )}
        </Svg>
        <View style={{alignItems:'center',backgroundColor:'rgba(0,0,0,0.85)',paddingHorizontal:18,paddingVertical:12,borderRadius:28}}>
          <Text style={{color:C.muted,fontSize:11,fontWeight:'700',letterSpacing:1.5,textTransform:'uppercase'}}>Activity</Text>
          <Text style={{color:C.green,fontSize:42,fontWeight:'800',lineHeight:48,marginTop:2}}>
            {pct!=null?Math.round(pct)+'%':'—'}
          </Text>
          <Text style={{color:C.muted,fontSize:11,marginTop:1}}>of in-view time moving</Text>
        </View>
      </View>
      <View style={{flexDirection:'row',gap:32,marginTop:16}}>
        <View style={{alignItems:'center'}}>
          <View style={{flexDirection:'row',alignItems:'center',gap:6,marginBottom:4}}>
            <View style={{width:8,height:8,borderRadius:4,backgroundColor:C.green}}/>
            <Text style={{color:C.sub,fontSize:12}}>Active</Text>
          </View>
          <Text style={{color:C.text,fontSize:18,fontWeight:'800'}}>
            {activeMin!=null?Math.round(activeMin)+' min':'—'}
          </Text>
        </View>
        <View style={{width:1,backgroundColor:C.border}}/>
        <View style={{alignItems:'center'}}>
          <View style={{flexDirection:'row',alignItems:'center',gap:6,marginBottom:4}}>
            <View style={{width:8,height:8,borderRadius:4,backgroundColor:C.orange}}/>
            <Text style={{color:C.sub,fontSize:12}}>Still</Text>
          </View>
          <Text style={{color:C.text,fontSize:18,fontWeight:'800'}}>
            {stillMin!=null?Math.round(stillMin)+' min':'—'}
          </Text>
        </View>
      </View>
    </View>
  );
}

// HEIGHT_PER_PERSON_V1: "5'6", "5 6", "66 in", "168", "1.68" -> centimetres.
function parseHeightCm(t:string):number|null{
  const x=(t||'').trim().toLowerCase();
  if(!x)return null;
  const ftin=x.match(/^(\d)\s*(?:'|ft|feet|\s)\s*(\d{1,2})?\s*(?:"|in|inches)?$/);
  if(ftin){const v=(parseInt(ftin[1],10)*12+(ftin[2]?parseInt(ftin[2],10):0))*2.54;return v>=50&&v<=230?Math.round(v):null;}
  const n=parseFloat(x);
  if(!isFinite(n))return null;
  let cm=n;
  if(x.endsWith('in'))cm=n*2.54; else if(n<3)cm=n*100; else if(n<=8)cm=n*30.48; else if(n<90&&!x.endsWith('cm'))cm=n*2.54;
  return cm>=50&&cm<=230?Math.round(cm):null;
}
const fmtHeight=(cm:number|null|undefined,units:'us'|'metric')=>{
  if(!cm)return'';
  if(units==='metric')return `${Math.round(cm)} cm`;
  const ti=Math.round(cm/2.54);return `${Math.floor(ti/12)}'${ti%12}"`;
};

// TEACH_V1: "Introduce everyone". One person at a time stands in front of the
// camera; the caregiver names them (or confirms the name VIGIL shows). Each tap
// teaches every identity cue: face, build, height, walk and clothing. Offered
// once after setup, and any time from Settings, Camera.
// WHO_SHEET_V1: "Who is this?" for one body the camera sees right now. It
// corrects that exact track (not everything filed under the wrong name), and
// can create someone new. Used on Today and in Introduce everyone.
function WhoIsThisSheet({serverUrl,track,current,people,onDone,onClose}:
  {serverUrl:string;track:number|null;current:{id:string;name:string|null}|null;
   people:{id:string;name:string|null}[];onDone:(p:{id:string;name:string},track:number|null)=>void;onClose:()=>void}){
  const [busy,setBusy]=useState(false);
  const [err,setErr]=useState<string|null>(null);
  const [newName,setNewName]=useState('');
  const [adding,setAdding]=useState(false);
  const named=people.filter(p=>!!p.name&&p.id!==current?.id);
  const pick=async(p:{id:string;name:string|null})=>{
    setBusy(true);setErr(null);
    try{
      if(current&&p.id===current.id)await AX.post(`${serverUrl}/persons/${p.id}/confirm`,{},{timeout:8000});
      else if(track!=null)await AX.post(`${serverUrl}/tracks/${track}/assign`,{target_person_id:p.id},{timeout:8000});
      else if(current)await AX.post(`${serverUrl}/persons/${current.id}/correct`,{target_person_id:p.id},{timeout:8000});
      onDone({id:p.id,name:p.name||'them'},track);
    }catch(e:any){setErr(e?.response?.data?.reason||'Could not reach the camera. Try again.');}
    finally{setBusy(false);}
  };
  const createNew=async()=>{
    const n=newName.trim();if(!n||track==null)return;
    setBusy(true);setErr(null);
    try{const r=await AX.post(`${serverUrl}/tracks/${track}/new`,{name:n,is_resident:false},{timeout:8000});
      onDone({id:r.data?.person_id,name:n},track);}
    catch(e:any){setErr(e?.response?.data?.reason||'Could not reach the camera. Try again.');}
    finally{setBusy(false);}
  };
  const row=(p:{id:string;name:string|null},label:string,primary=false)=>(
    <TouchableOpacity key={p.id+label} disabled={busy} onPress={()=>pick(p)}
      style={{flexDirection:'row',alignItems:'center',padding:12,borderRadius:12,marginTop:8,backgroundColor:primary?C.green+'22':C.fill,opacity:busy?0.5:1}}>
      <PersonAvatar personId={p.id} name={p.name} serverUrl={serverUrl} size={36}/>
      <Text style={{color:C.text,fontSize:16,fontWeight:'600',marginLeft:12,flex:1}}>{label}</Text>
    </TouchableOpacity>);
  return(
    <Modal visible transparent animationType="slide" onRequestClose={onClose}>
      <KeyboardAvoidingView style={{flex:1,justifyContent:'flex-end',backgroundColor:'#000A'}} behavior={Platform.OS==='ios'?'padding':undefined}>
        <View style={{backgroundColor:C.card,borderTopLeftRadius:20,borderTopRightRadius:20,padding:20,paddingBottom:36,maxHeight:'85%'}}>
          <Text style={{color:C.text,fontSize:21,fontWeight:'800'}}>{current?.name?`Is this ${current.name}?`:'Who is this?'}</Text>
          <Text style={{color:C.sub,fontSize:14,marginTop:4,lineHeight:20}}>Only this person on the camera is changed. VIGIL learns from your answer.</Text>
          <ScrollView style={{marginTop:6}} keyboardShouldPersistTaps="handled">
            {current?.name&&row(current,`Yes, this is ${current.name}`,true)}
            {current?.name&&named.length>0&&<Text style={{color:C.muted,fontSize:13,marginTop:14}}>No, it is:</Text>}
            {named.map(p=>row(p,p.name||'?'))}
            {track!=null&&(adding?(
              <View style={{marginTop:10}}>
                <TextInput value={newName} onChangeText={setNewName} placeholder="Their name" placeholderTextColor={C.muted} autoFocus autoCapitalize="words"
                  style={{color:C.text,fontSize:17,padding:14,backgroundColor:C.bg,borderRadius:10,borderWidth:1,borderColor:C.border}}/>
                <TouchableOpacity disabled={busy||!newName.trim()} onPress={createNew} style={[s.btnPrimary,{marginTop:10,opacity:(busy||!newName.trim())?0.5:1}]}>
                  <Text style={s.btnPrimaryTxt}>Save {newName.trim()||'new person'}</Text></TouchableOpacity>
              </View>
            ):(
              <TouchableOpacity onPress={()=>setAdding(true)} style={{padding:14,borderRadius:12,marginTop:8,borderWidth:1.5,borderStyle:'dashed',borderColor:C.green,alignItems:'center'}}>
                <Text style={{color:C.green,fontSize:16,fontWeight:'700'}}>+ Someone new</Text></TouchableOpacity>))}
          </ScrollView>
          {!!err&&<Text style={{color:C.red,fontSize:14,marginTop:10}}>{err}</Text>}
          <TouchableOpacity onPress={onClose} style={{alignItems:'center',paddingTop:16}}><Text style={{color:C.sub,fontSize:16}}>Cancel</Text></TouchableOpacity>
        </View>
      </KeyboardAvoidingView>
    </Modal>);
}

// ENROLL_V1: after someone is named, VIGIL keeps reading them (face, clothing from
// every side, build, height, walk) and only shows the check once it really has them.
const EN_PARTS:[string,string][]=[['face','Face'],['front','Clothing, front'],['side','Clothing, side'],
  ['back','Clothing, back'],['body','Build'],['height','Height'],['walk','Walk']];
const EN_STAGES:[string,string][]=[['still','Stand still'],['turn','Turn around'],['walk','Walk']];
function EnrollRing({serverUrl,personId,personName,track,frame,onDone,onCancel}:
  {serverUrl:string;personId:string;personName:string;track:number|null;frame:string|null;
   onDone:(learned:string[],another:boolean)=>void;onCancel:()=>void}){
  const [st,setSt]=useState<any>(null);
  const [err,setErr]=useState<string|null>(null);
  const [othersSeen,setOthersSeen]=useState(0);     // ENROLL_V2: polls in a row with someone else in view
  const keyRef=useRef<string|null>(null);
  const spin=useRef(new Animated.Value(0)).current;
  const pop=useRef(new Animated.Value(0)).current;
  const done=!!st?.done;
  useEffect(()=>{
    const loop=Animated.loop(Animated.timing(spin,{toValue:1,duration:1400,useNativeDriver:true,easing:(t:number)=>t}));
    loop.start();return()=>loop.stop();
  },[spin]);
  useEffect(()=>{if(done)Animated.spring(pop,{toValue:1,useNativeDriver:true,friction:5,tension:90}).start();},[done,pop]);
  useEffect(()=>{
    let alive=true;let stale=0;
    (async()=>{
      try{const r=await AX.post(`${serverUrl}/enroll/start`,{person_id:personId,track},{timeout:8000});keyRef.current=r.data?.key??null;}
      catch(_e){if(alive)setErr('Could not reach the camera. Check it is online, then try again.');return;}
      while(alive){
        try{
          const r=await AX.get(`${serverUrl}/enroll/status`,{params:{key:keyRef.current},timeout:4000});
          if(!alive)break;
          if(r.data?.active){stale=0;setErr(null);setSt(r.data);setOthersSeen(n=>r.data.others_in_view>0?n+1:0);if(r.data.done)break;}
          else{stale++;if(stale>=16)setErr(r.data?.camera_running===false
            ?'The camera is not sending video right now. Check it is plugged in and online.'
            :'Waiting for the camera to start learning...');}
        }catch(_e){stale++;if(stale>=16&&alive)setErr('Lost the connection to the camera. Retrying...');}
        await new Promise<void>(res=>setTimeout(()=>res(),500));
      }
    })();
    return()=>{alive=false;};
  },[serverUrl,personId,track]);
  const cancel=()=>{AX.post(`${serverUrl}/enroll/stop`,{},{timeout:5000}).catch(()=>{});onCancel();};
  const finish=(another:boolean)=>{AX.post(`${serverUrl}/enroll/stop`,{},{timeout:5000}).catch(()=>{});onDone(st?.learned||[],another);};
  const R=86,SW=10,CIRC=2*Math.PI*R;
  const prog=Math.max(0,Math.min(1,st?.progress??0));
  const parts=st?.parts||{};
  const stageIdx=done?3:EN_STAGES.findIndex(x=>x[0]===st?.stage);
  const rot=spin.interpolate({inputRange:[0,1],outputRange:['0deg','360deg']});
  const missing=done?EN_PARTS.filter(([k])=>parts[k]!=null&&parts[k]<1).map(([,l])=>l.toLowerCase()):[];
  return(
    <Modal visible animationType="fade" onRequestClose={cancel}>
      <SafeAreaView style={{flex:1,backgroundColor:C.bg}}>
        <ScrollView contentContainerStyle={{padding:20,paddingBottom:40,alignItems:'center'}}>
          <Text style={{color:C.text,fontSize:24,fontWeight:'800',alignSelf:'flex-start'}}>{done?`VIGIL knows ${personName}`:`Learning ${personName}`}</Text>
          <Text style={{color:C.sub,fontSize:14,marginTop:6,lineHeight:20,alignSelf:'flex-start'}}>
            {done?'Every part is saved. VIGIL keeps improving each time you confirm or correct a name.'
              :`Only ${personName} in front of the camera. This takes about a minute; the circle fills as VIGIL learns each part.`}
          </Text>
          {!!frame&&!done&&(
            <View style={{marginTop:14,width:'100%',borderRadius:12,overflow:'hidden',backgroundColor:'#000',aspectRatio:16/9}}>
              <Image source={{uri:`data:image/jpeg;base64,${frame}`}} style={{width:'100%',height:'100%'}} resizeMode="contain"/>
            </View>)}
          <View style={{width:2*R+SW+8,height:2*R+SW+8,marginTop:22,alignItems:'center',justifyContent:'center'}}>
            <Svg width={2*R+SW+8} height={2*R+SW+8} style={{position:'absolute'}}>
              <Circle cx={R+SW/2+4} cy={R+SW/2+4} r={R} stroke={C.fill} strokeWidth={SW} fill="none"/>
              <Circle cx={R+SW/2+4} cy={R+SW/2+4} r={R} stroke={C.green} strokeWidth={SW} fill="none"
                strokeLinecap="round" strokeDasharray={`${CIRC*prog} ${CIRC}`}
                transform={`rotate(-90 ${R+SW/2+4} ${R+SW/2+4})`}/>
            </Svg>
            {!done&&(
              <Animated.View style={{position:'absolute',width:2*R+SW+8,height:2*R+SW+8,transform:[{rotate:rot}]}}>
                <Svg width={2*R+SW+8} height={2*R+SW+8}>
                  <Circle cx={R+SW/2+4} cy={R+SW/2+4} r={R+SW/2+2} stroke={C.green+'66'} strokeWidth={2} fill="none"
                    strokeLinecap="round" strokeDasharray={`${CIRC*0.12} ${CIRC}`}/>
                </Svg>
              </Animated.View>)}
            {done?(
              <Animated.View style={{width:96,height:96,borderRadius:48,backgroundColor:C.green,alignItems:'center',justifyContent:'center',
                transform:[{scale:pop}]}}>
                <Svg width={52} height={52} viewBox="0 0 52 52">
                  <Path d="M12 27 L22 37 L41 16" stroke="#000" strokeWidth={6} fill="none" strokeLinecap="round" strokeLinejoin="round"/>
                </Svg>
              </Animated.View>
            ):(
              <Text style={{color:C.text,fontSize:30,fontWeight:'800',fontVariant:['tabular-nums']}}>{Math.round(prog*100)}%</Text>
            )}
          </View>
          <Text style={{color:done?C.green:(st&&!st.in_view)?C.orange:C.text,fontSize:19,fontWeight:'700',marginTop:20,textAlign:'center'}}>
            {done?'Done':err&&!st?'Connecting...':st?.prompt??'Getting ready...'}
          </Text>
          {!done&&othersSeen>=6&&(
            <Text style={{color:C.orange,fontSize:14,marginTop:8,textAlign:'center'}}>Someone else is in view. Ask them to step out for a minute.</Text>)}
          {!!err&&<Text style={{color:C.orange,fontSize:14,marginTop:8,textAlign:'center',lineHeight:20}}>{err}</Text>}
          <View style={{flexDirection:'row',marginTop:18,gap:8}}>
            {EN_STAGES.map(([k,l],i)=>(
              <View key={k} style={{paddingHorizontal:12,paddingVertical:7,borderRadius:16,
                backgroundColor:i<stageIdx?C.green+'22':i===stageIdx?C.fill:'transparent',borderWidth:1,borderColor:i<=stageIdx?C.green+'66':C.border}}>
                <Text style={{color:i<stageIdx?C.green:i===stageIdx?C.text:C.muted,fontSize:13,fontWeight:'700'}}>{i<stageIdx?'✓ ':''}{l}</Text>
              </View>))}
          </View>
          <View style={[s.card,{marginTop:18,width:'100%'}]}>
            {EN_PARTS.filter(([k])=>parts[k]!=null||!st).map(([k,l])=>{
              const v=parts[k]??0;const ok=v>=1;
              return(
                <View key={k} style={{flexDirection:'row',alignItems:'center',paddingVertical:7}}>
                  <View style={{width:22,height:22,borderRadius:11,marginRight:12,alignItems:'center',justifyContent:'center',
                    backgroundColor:ok?C.green:'transparent',borderWidth:ok?0:2,borderColor:C.border}}>
                    {ok&&<Text style={{color:'#000',fontSize:12,fontWeight:'900'}}>{'✓'}</Text>}
                  </View>
                  <Text style={{color:ok?C.text:C.sub,fontSize:15,flex:1}}>{l}</Text>
                  {!ok&&<View style={{width:70,height:5,borderRadius:3,backgroundColor:C.fill}}>
                    <View style={{width:`${Math.round(v*100)}%` as any,height:5,borderRadius:3,backgroundColor:C.green}}/></View>}
                </View>);
            })}
          </View>
          {done&&missing.length>0&&(
            <Text style={{color:C.muted,fontSize:13,marginTop:10,lineHeight:19,alignSelf:'flex-start'}}>
              Not fully seen: {missing.join(', ')}. That is fine: VIGIL fills it in as {personName} moves around.
            </Text>)}
          {done?(<>
            <TouchableOpacity onPress={()=>finish(false)} style={[s.btnPrimary,{marginTop:22,alignSelf:'stretch'}]}>
              <Text style={s.btnPrimaryTxt}>Save</Text>
            </TouchableOpacity>
            <TouchableOpacity onPress={()=>finish(true)} style={{marginTop:12,alignSelf:'stretch',padding:15,borderRadius:14,borderWidth:1,borderColor:C.green,alignItems:'center'}}>
              <Text style={{color:C.green,fontSize:16,fontWeight:'700'}}>Save and introduce someone else</Text>
            </TouchableOpacity>
          </>):(
            <TouchableOpacity onPress={cancel} style={{marginTop:22,padding:12}}>
              <Text style={{color:C.sub,fontSize:15}}>Stop. {personName} stays named, VIGIL learns more over time.</Text>
            </TouchableOpacity>)}
        </ScrollView>
      </SafeAreaView>
    </Modal>);
}

function TeachWhoScreen({serverUrl,units,onClose}:{serverUrl:string;units:'us'|'metric';onClose:()=>void}){
  const [named,setNamed]=useState<{id:string;name:string|null;track?:number|null}[]>([]);
  const [who,setWho]=useState<{track:number|null;current:{id:string;name:string|null}|null}|null>(null);   // WHO_SHEET_V1
  const [others,setOthers]=useState<{label:string;track:number|null}[]>([]);
  const [everyone,setEveryone]=useState<{id:string;name:string|null;height_cm?:number|null}[]>([]);
  const [done,setDone]=useState<string[]>([]);
  const [editing,setEditing]=useState<number|null>(null);
  const [newName,setNewName]=useState('');
  const [newHeight,setNewHeight]=useState('');
  const [isRes,setIsRes]=useState(false);
  const [busy,setBusy]=useState(false);
  const [msg,setMsg]=useState<string|null>(null);
  const [frame,setFrame]=useState<string|null>(null);
  const [enrolling,setEnrolling]=useState<{id:string;name:string;track:number|null}|null>(null);   // ENROLL_V1
  useEffect(()=>{                     // live picture, with the names VIGIL is showing
    let alive=true;
    (async()=>{while(alive){
      try{const r=await AX.get(`${serverUrl}/camera/snapshot?w=800`,{timeout:4000});
        if(alive&&r.data?.available&&r.data?.frame)setFrame(r.data.frame);}catch(_e){}
      await new Promise(res=>setTimeout(res,350));
    }})();
    return()=>{alive=false;};
  },[serverUrl]);
  useEffect(()=>{
    let alive=true;
    const tick=async()=>{
      try{
        const r=await AX.get(`${serverUrl}/persons/in-view`,{timeout:3000});
        if(!alive)return;
        setNamed((r.data?.people||[]).filter((p:any)=>!!p.name));
        setOthers((r.data?.others||[]).filter((o:any)=>o.track!=null));
      }catch(_e){}
    };
    const loadAll=async()=>{try{const r=await AX.get(`${serverUrl}/persons`,{timeout:5000});if(alive&&Array.isArray(r.data))setEveryone(r.data.filter((p:any)=>!!p.name));}catch(_e){}};
    tick();loadAll();
    const a=setInterval(tick,1500),b=setInterval(loadAll,8000);
    return()=>{alive=false;clearInterval(a);clearInterval(b);};
  },[serverUrl]);
  const learned=(n:string)=>{setDone(d=>d.includes(n)?d:[...d,n]);setMsg(`Saved ${n}. Next person, or Save.`);};
  const confirm=async(p:{id:string;name:string|null})=>{
    setBusy(true);
    try{await AX.post(`${serverUrl}/persons/${p.id}/confirm`,{},{timeout:8000});setMsg(null);setEnrolling({id:p.id,name:p.name||'them',track:(p as any).track??null});}
    catch(_e){setMsg('Could not reach the camera. Try again.');}
    finally{setBusy(false);}
  };
  const assign=async(track:number,p:{id:string;name:string|null})=>{
    setBusy(true);
    try{await AX.post(`${serverUrl}/tracks/${track}/assign`,{target_person_id:p.id},{timeout:8000});setEditing(null);setMsg(null);setEnrolling({id:p.id,name:p.name||'them',track});}
    catch(_e){setMsg('Could not reach the camera. Try again.');}
    finally{setBusy(false);}
  };
  const createNew=async(track:number)=>{
    const n=newName.trim();if(!n)return;
    const h=newHeight.trim()?parseHeightCm(newHeight):null;
    if(newHeight.trim()&&h==null){setMsg(units==='us'?'Height like 5\'6" or 66 in.':'Height like 168 cm.');return;}
    setBusy(true);
    try{
      const r=await AX.post(`${serverUrl}/tracks/${track}/new`,{name:n,height_cm:h,is_resident:isRes},{timeout:8000});
      setEditing(null);setNewName('');setNewHeight('');setIsRes(false);setMsg(null);
      if(r.data?.person_id)setEnrolling({id:r.data.person_id,name:n,track});else learned(n);
    }catch(e:any){setMsg(e?.response?.data?.reason||'Could not reach the camera. Try again.');}
    finally{setBusy(false);}
  };
  const chip=(label:string,onPress:()=>void,primary=false)=>(
    <TouchableOpacity key={label} disabled={busy} onPress={onPress}
      style={{paddingHorizontal:14,paddingVertical:10,borderRadius:20,marginRight:8,marginTop:8,
        backgroundColor:primary?C.green:C.fill,opacity:busy?0.5:1}}>
      <Text style={{color:primary?'#000':C.text,fontWeight:'700',fontSize:14}}>{label}</Text>
    </TouchableOpacity>);
  const inp={color:C.text,fontSize:16,paddingHorizontal:12,paddingVertical:12,backgroundColor:C.bg,borderRadius:10,borderWidth:1,borderColor:C.border,marginTop:8};
  return(
    <SafeAreaView style={{flex:1,backgroundColor:C.bg}}>
      {who&&<WhoIsThisSheet serverUrl={serverUrl} track={who.track} current={who.current}
        people={everyone} onClose={()=>setWho(null)}
        onDone={(p,t)=>{setWho(null);setEnrolling({id:p.id,name:p.name,track:t});}}/>}
      {enrolling&&<EnrollRing serverUrl={serverUrl} personId={enrolling.id} personName={enrolling.name} track={enrolling.track}
        frame={frame} onDone={(_l,another)=>{const n=enrolling.name;setEnrolling(null);learned(n);
          if(another)setMsg(`Saved ${n}. Next person: stand in front of the camera, then tap their name below.`);else onClose();}}
        onCancel={()=>{const n=enrolling.name;setEnrolling(null);setDone(d=>d.includes(n)?d:[...d,n]);setMsg(`${n} is named. VIGIL keeps learning them over time.`);}}/>}
      <KeyboardAvoidingView style={{flex:1}} behavior={Platform.OS==='ios'?'padding':undefined}>
      <ScrollView contentContainerStyle={{padding:20,paddingBottom:60}} keyboardShouldPersistTaps="handled">
        <Text style={{color:C.text,fontSize:26,fontWeight:'800'}}>Introduce everyone</Text>
        <Text style={{color:C.sub,fontSize:15,marginTop:8,lineHeight:22}}>
          About a minute per person. Recommended: VIGIL then tells your family apart by face, build, height, walk and clothing, and keeps learning every time you name someone.
        </Text>
        <View style={[s.card,{marginTop:16}]}>
          <Text style={{color:C.text,fontSize:15,fontWeight:'700'}}>For each person, one at a time</Text>
          <Text style={{color:C.sub,fontSize:14,marginTop:6,lineHeight:21}}>
            1. Stand 2 to 3 metres from the camera, facing it, in good light.{'\n'}
            2. Stay still for about 10 seconds until they appear below.{'\n'}
            3. Tap their name. VIGIL then studies them for about a minute: stand still, turn around once, walk across the room. A check appears when it knows them.{'\n'}
            4. Then the next person.
          </Text>
        </View>
        <View style={{marginTop:16,borderRadius:14,overflow:'hidden',backgroundColor:'#000',aspectRatio:16/9,alignItems:'center',justifyContent:'center'}}>
          {frame
            ?<Image source={{uri:`data:image/jpeg;base64,${frame}`}} style={{width:'100%',height:'100%'}} resizeMode="contain"/>
            :<ActivityIndicator color={C.green}/>}
        </View>
        <Text style={{color:C.muted,fontSize:12,marginTop:6}}>Live camera. The label above each person is what VIGIL calls them now.</Text>
        {!!msg&&<Text style={{color:C.green,fontSize:14,marginTop:14}}>{msg}</Text>}
        <Text style={[s.secHeader,{marginHorizontal:0,marginTop:18}]}>In view now</Text>
        {named.length===0&&others.length===0&&(
          <Text style={{color:C.muted,fontSize:14,lineHeight:20}}>Nobody in view yet. Stand in front of the camera.</Text>)}
        {named.map(p=>(
          <View key={p.id} style={[s.card,{marginTop:8}]}>
            <Text style={{color:C.text,fontSize:16,fontWeight:'700'}}>VIGIL thinks this is {p.name}</Text>
            <View style={{flexDirection:'row',flexWrap:'wrap'}}>
              {chip(`Yes, ${p.name}`,()=>confirm(p),true)}
              {chip('No, someone else',()=>{setMsg(null);setWho({track:p.track??null,current:p});})}
            </View>
          </View>))}
        {others.map((o,i)=>(
          <View key={'o'+i} style={[s.card,{marginTop:8}]}>
            <Text style={{color:C.text,fontSize:16,fontWeight:'700'}}>{o.label==='Identifying...'?'Someone new':o.label}: who is this?</Text>
            <View style={{flexDirection:'row',flexWrap:'wrap'}}>
              {everyone.slice(0,8).map(p=>chip(p.name||'?',()=>assign(o.track as number,p)))}
              {chip('A new person',()=>{setEditing(o.track);setNewName('');setNewHeight('');},true)}
            </View>
            {editing===o.track&&(
              <View style={{marginTop:6}}>
                <TextInput style={inp} value={newName} onChangeText={setNewName} placeholder="Name" placeholderTextColor={C.muted} autoCapitalize="words"/>
                <TextInput style={inp} value={newHeight} onChangeText={setNewHeight}
                  placeholder={units==='us'?'Height, e.g. 5\'6" (optional)':'Height in cm (optional)'} placeholderTextColor={C.muted}/>
                <View style={{flexDirection:'row',alignItems:'center',marginTop:12}}>
                  <Switch value={isRes} onValueChange={setIsRes} trackColor={{false:C.fill,true:C.green}} thumbColor="#FFF"/>
                  <Text style={{color:C.sub,fontSize:14,marginLeft:10,flex:1}}>This is a person VIGIL looks after (a resident)</Text>
                </View>
                <TouchableOpacity disabled={busy||!newName.trim()} onPress={()=>createNew(o.track as number)}
                  style={[s.btnPrimary,{marginTop:12,opacity:(busy||!newName.trim())?0.5:1}]}>
                  <Text style={s.btnPrimaryTxt}>Save {newName.trim()||'this person'}</Text>
                </TouchableOpacity>
              </View>)}
          </View>))}
        {done.length>0&&<Text style={{color:C.sub,fontSize:14,marginTop:16}}>Learned so far: {done.join(', ')}</Text>}
        <TouchableOpacity onPress={onClose} style={[s.btnPrimary,{marginTop:24}]}>
          <Text style={s.btnPrimaryTxt}>{done.length?'Save':'Skip for now'}</Text>
        </TouchableOpacity>
        <Text style={{color:C.muted,fontSize:12,textAlign:'center',marginTop:10}}>You can do this any time: Settings, Camera, Introduce everyone.</Text>
      </ScrollView>
      </KeyboardAvoidingView>
    </SafeAreaView>
  );
}

// ─────────────────────────────────────────────
// BASELINE_V2: "Is this normal for them?"
// Baseline is VIGIL's memory of each person's own normal. Every measure gets
// one plain verdict and one bar: the green zone is their usual range (from
// the days before this week), the dot is this week's typical value. AI
// Insights explains what a change might mean; this page shows the numbers it
// stands on.
// ─────────────────────────────────────────────
type BLMeasure={key:string;label:string;unit:string;dec:number;higherWorse?:boolean;why:string;
  series:{date:string;v:number|null}[]};
const BL_MIN_DAYS=5;
const blMedian=(a:number[])=>{const b=[...a].sort((x,y)=>x-y);const n=b.length;return n%2?b[(n-1)/2]:(b[n/2-1]+b[n/2])/2;};
function blEvaluate(m:BLMeasure){
  const today=new Date();const wk=new Date(today);wk.setDate(today.getDate()-6);
  const cut=wk.toISOString().slice(0,10);
  const pts=m.series.filter(x=>x.v!=null&&isFinite(x.v as number)) as {date:string;v:number}[];
  const before=pts.filter(x=>x.date<cut).slice(-30).map(x=>x.v);
  const week=pts.filter(x=>x.date>=cut).map(x=>x.v);
  if(before.length<BL_MIN_DAYS)return{state:'learning' as const,daysBefore:before.length,weekVal:week.length?blMedian(week):null};
  const med=blMedian(before);
  const mad=blMedian(before.map(v=>Math.abs(v-med)));
  const sd=Math.max(1.4826*mad,Math.abs(med)*0.03);
  const lo=med-1.5*sd,hi=med+1.5*sd;
  if(week.length<2)return{state:'noweek' as const,lo,hi,med,daysBefore:before.length,weekVal:week.length?week[0]:null};
  const wv=blMedian(week);
  const where=wv<lo?'low':wv>hi?'high':'normal';
  const concern=where!=='normal'&&((where==='high')===!!m.higherWorse);
  return{state:'ok' as const,lo,hi,med,wv,where,concern,daysBefore:before.length,daysWeek:week.length};
}
function BaselineMeasureCard({m}:{m:BLMeasure}){
  const e:any=blEvaluate(m);
  const f=(v:number)=>v.toFixed(m.dec);
  const pill=(txt:string,col:string)=>(
    <View style={{paddingHorizontal:10,paddingVertical:4,borderRadius:12,backgroundColor:col+'22'}}>
      <Text style={{color:col,fontSize:12,fontWeight:'700'}}>{txt}</Text></View>);
  let status=pill('Learning',C.sub), line='', bar=null as any;
  if(e.state==='learning'){
    line=`Learning their normal: ${e.daysBefore} of ${BL_MIN_DAYS} days recorded.`;
  }else{
    const verdict=e.state!=='ok'?pill('Not enough this week',C.sub)
      :e.where==='normal'?pill('Normal for them',C.green)
      :pill(e.where==='low'?'Lower than usual':'Higher than usual',e.concern?C.orange:C.blue);
    status=verdict;
    line=e.state==='ok'
      ?`This week ${f(e.wv)} ${m.unit}. Usual ${f(e.lo)} to ${f(e.hi)} ${m.unit}.`
      :`Usual ${f(e.lo)} to ${f(e.hi)} ${m.unit}. Not seen enough this week to compare.`;
    // one bar: the usual range in green, this week as a dot
    const v=e.state==='ok'?e.wv:null;
    const span=(e.hi-e.lo)||1;
    const left=Math.min(e.lo,v??e.lo)-span*0.6, right=Math.max(e.hi,v??e.hi)+span*0.6;
    const pct=(x:number)=>`${Math.max(0,Math.min(100,((x-left)/(right-left))*100))}%`;
    const dotCol=e.state!=='ok'?C.sub:e.where==='normal'?C.green:e.concern?C.orange:C.blue;
    bar=(
      <View style={{marginTop:14,marginBottom:4}}>
        <View style={{height:10,borderRadius:5,backgroundColor:C.fill}}>
          <View style={{position:'absolute',top:0,bottom:0,left:pct(e.lo) as any,
            width:`${((e.hi-e.lo)/(right-left))*100}%` as any,backgroundColor:C.green+'55',borderRadius:5}}/>
          {v!=null&&<View style={{position:'absolute',top:-5,left:pct(v) as any,marginLeft:-10,width:20,height:20,borderRadius:10,
            backgroundColor:dotCol,borderWidth:3,borderColor:C.card}}/>}
        </View>
        <View style={{flexDirection:'row',justifyContent:'space-between',marginTop:8}}>
          <Text style={{color:C.muted,fontSize:11}}>{m.higherWorse?'Better':'Lower'}</Text>
          <Text style={{color:C.green,fontSize:11}}>Usual range</Text>
          <Text style={{color:C.muted,fontSize:11}}>{m.higherWorse?'Worse':'Higher'}</Text>
        </View>
      </View>);
  }
  return(
    <View style={{marginHorizontal:16,marginBottom:10,padding:16,backgroundColor:C.card,borderRadius:16}}>
      <View style={{flexDirection:'row',justifyContent:'space-between',alignItems:'center'}}>
        <Text style={{color:C.text,fontSize:15,fontWeight:'700',flex:1,paddingRight:8}}>{m.label}</Text>
        {status}
      </View>
      <Text style={{color:C.sub,fontSize:13,marginTop:6,lineHeight:19}}>{line}</Text>
      {bar}
      <Text style={{color:C.muted,fontSize:12,marginTop:8,lineHeight:17}}>{m.why}</Text>
    </View>
  );
}
function BaselineView({name,measures,groups}:{name:string;measures:BLMeasure[];groups?:{title:string;keys:string[]}[]}){
  const shown=measures.filter(m=>m.series.some(x=>x.v!=null));
  const ev=shown.map(m=>({m,e:blEvaluate(m) as any}));
  const ready=ev.filter(x=>x.e.state==='ok');
  const off=ready.filter(x=>x.e.where!=='normal');
  const days=Math.max(0,...ev.map(x=>x.e.daysBefore||0));
  const learning=ev.length>0&&ev.every(x=>x.e.state==='learning');
  const head=learning?`Learning ${name}'s normal`
    :!ready.length?`${name}'s normal is set`
    :off.length===0?`Everything is normal for ${name} this week`
    :`${off.length} of ${ready.length} ${off.length===1?'measure is':'measures are'} outside ${name}'s normal`;
  const sub=learning
    ?`VIGIL needs ${BL_MIN_DAYS} days of ${name} walking in view before it can say what is normal for them. ${days} so far.`
    :`Compared with ${name}'s own usual range from up to ${Math.min(30,days)} earlier days, never with other people. AI Insights explains what a change may mean.`;
  const byKey:Record<string,BLMeasure>={};shown.forEach(m=>{byKey[m.key]=m;});
  const sections=groups||[{title:'',keys:shown.map(m=>m.key)}];
  return(
    <View>
      <View style={{marginHorizontal:16,marginBottom:14,padding:16,backgroundColor:C.card,borderRadius:16,
        borderWidth:1,borderColor:(!learning&&off.some(x=>x.e.concern))?C.orange+'66':C.border}}>
        <Text style={{color:C.text,fontSize:17,fontWeight:'800',lineHeight:23}}>{head}</Text>
        <Text style={{color:C.sub,fontSize:13,marginTop:6,lineHeight:19}}>{sub}</Text>
        {!learning&&off.length>0&&<Text style={{color:C.text,fontSize:13,marginTop:8,lineHeight:19}}>
          {off.map(x=>`${x.m.label}: ${x.e.where==='low'?'lower':'higher'} than usual`).join('. ')+'.'}</Text>}
      </View>
      {shown.length===0&&<View style={s.empty}><Text style={s.emptyTxt}>No measurements yet</Text>
        <Text style={s.emptySub}>Numbers appear after {name} is seen walking by the camera</Text></View>}
      {sections.map(g=>{
        const ms=g.keys.map(k=>byKey[k]).filter(Boolean);
        if(!ms.length)return null;
        return(<View key={g.title||'all'}>
          {!!g.title&&<Text style={s.secHeader}>{g.title}</Text>}
          {ms.map(m=><BaselineMeasureCard key={m.key} m={m}/>)}
        </View>);
      })}
    </View>
  );
}
const BL_WHY:Record<string,string>={
  speed:'Walking more slowly over weeks is one of the best-studied early signs of falls and declining health.',
  cadence:'Fewer steps per minute can come with tiredness, pain or weakness.',
  posture:'Stooping more over time can come with back pain, weakness or fatigue.',
  asym:'Favouring one leg can mean pain or a new problem on one side.',
  stride:'Less regular steps are linked to a higher chance of falling.',
  activity:'Moving much less than usual can be an early sign of illness or low mood.',
  active:'Fewer active minutes than usual is worth noticing if it lasts.',
  still:'Much longer still periods than usual are worth a check.',
  rhr:'A resting heart rate above their usual can come with illness, pain or poor sleep.',
  hrv:'Lower than their usual can mean stress, illness or poor recovery.',
  sleep:'A lasting change in sleep is worth noticing.',
};

// ─────────────────────────────────────────────
// AI INSIGHTS SCREEN
// ─────────────────────────────────────────────
function PersonDetailScreen({
  personId, personName, serverUrl, onClose, demoMode, embedded, view='week', watchHistory=null,
}:{personId:string; personName:string; serverUrl:string; onClose:()=>void;
   demoMode?:boolean; embedded?:boolean; view?:'week'|'baseline';
   watchHistory?:DailySummary[]|null}) {   // WATCH_OWNER_V2: only passed for the person who wears the watch
  const [trends,setTrends]         = useState<PersonTrendDay[]>([]);
  const [baseline,setBaseline]     = useState<PersonBaselineStatus|null>(null);
  const [weeklyTrend,setWeeklyTrend] = useState<any>(null);
  const [pdLoading,setPdLoading]   = useState(true);

  const load = useCallback(async()=>{
    if(demoMode){
      setTrends(DEMO_PTRENDS); setBaseline(DEMO_PBASELINE);
      setWeeklyTrend(DEMO_WEEKLY); setPdLoading(false);
      return;
    }
    setPdLoading(true);
    const u = serverUrl||FALLBACK_URL;
    try{
      const [t,b,w] = await Promise.all([
        AX.get(`${u}/persons/${personId}/detections?days=60`,{timeout:8000}),
        AX.get(`${u}/persons/${personId}/baseline-status`,{timeout:8000}),
        AX.get(`${u}/persons/${personId}/weekly-trend`,{timeout:8000}).catch(()=>({data:null})),
      ]);
      if(Array.isArray(t.data))setTrends(t.data);
      if(b.data)setBaseline(b.data);
      if(w.data)setWeeklyTrend(w.data);
    }catch(_){}
    finally{setPdLoading(false);}
  },[serverUrl,personId,demoMode]);

  useEffect(()=>{load();},[load]);

  const PTrendBar=({data,label,unit,color,dec=1}:{data:(number|null)[];label:string;unit:string;color:string;dec?:number})=>{
    const valid=data.filter(v=>v!=null) as number[];
    if(!valid.length)return null;
    const mean=valid.reduce((a,b)=>a+b,0)/valid.length;
    const min=Math.min(...valid); const max=Math.max(...valid); const range=max-min||1;
    const latest=valid[valid.length-1];
    const BAR=44;
    return(
      <View style={{marginHorizontal:16,marginBottom:10,padding:14,backgroundColor:C.card,borderRadius:16}}>
        <View style={{flexDirection:'row',justifyContent:'space-between',marginBottom:8}}>
          <View>
            <Text style={{color:C.sub,fontSize:12.5,fontWeight:'600',marginBottom:2}}>{label}</Text>
            <Text style={{color,fontSize:20,fontWeight:'800'}}>{latest!=null?latest.toFixed(dec):'—'}{' '}<Text style={{color:C.sub,fontSize:11}}>{unit}</Text></Text>
          </View>
          <Text style={{color:C.muted,fontSize:11}}>{personName}'s avg {mean.toFixed(dec)}</Text>
        </View>
        <View style={{height:BAR,flexDirection:'row',alignItems:'flex-end',gap:3}}>
          {data.map((v,i)=>{
            if(v==null)return <View key={i} style={{flex:1,height:3,backgroundColor:C.muted+'20',borderRadius:2}}/>;
            const h=Math.max(4,((v-min)/range)*BAR);
            return <View key={i} style={{flex:1,height:h,backgroundColor:color,borderRadius:3,opacity:i===data.length-1?1:0.6}}/>;
          })}
        </View>
      </View>
    );
  };

  const daysCollected = baseline?.days_collected ?? trends.length;
  const daysNeeded = baseline?.days_needed ?? 7;
  const hasBaseline = baseline?.has_baseline ?? (daysCollected>=daysNeeded);
  const progressPct = Math.min(100,(daysCollected/Math.max(1,daysNeeded))*100);

  // TRENDS_INLINE_V1: the same person's 7-day and baseline, shown inside Trends.
  if(embedded){
    if(pdLoading)return <View style={{paddingVertical:40,alignItems:'center'}}><ActivityIndicator color={C.green}/></View>;
    const cutoff=new Date(); cutoff.setDate(cutoff.getDate()-6);
    const cut=cutoff.toISOString().slice(0,10);
    const wk=trends.filter(t=>t.date>=cut);
    const watchAll=[...(watchHistory??[])].sort((a,b)=>a.date<b.date?-1:1);
    const watchWk=watchAll.filter(r=>r.date>=cut&&(r.step_count!=null||r.resting_hr!=null||r.hrv_sdnn!=null||r.sleep_hours!=null));
    const stat=(k:'avg_speed'|'avg_cadence'|'avg_posture'|'avg_asymmetry')=>{
      const b=baseline?.baseline?.[k];
      if(b)return {mean:b.mean,std:b.std,src:'baseline'};
      const v=trends.map(t=>t[k]).filter(x=>x!=null) as number[];
      if(v.length<3)return null;
      const m=v.reduce((a,b2)=>a+b2,0)/v.length;
      return {mean:m,std:Math.sqrt(v.reduce((a,b2)=>a+(b2-m)**2,0)/v.length),src:'sofar'};
    };
    const rows:[string,'avg_speed'|'avg_cadence'|'avg_posture'|'avg_asymmetry',string,number][]=[
      ['Walking Speed','avg_speed','m/s',2],['Cadence','avg_cadence','spm',0],
      ['Posture Score','avg_posture','/100',0],['Gait Asymmetry','avg_asymmetry','%',1]];
    const latest=(k:'avg_speed'|'avg_cadence'|'avg_posture'|'avg_asymmetry')=>{const v=trends.map(t=>t[k]).filter(x=>x!=null) as number[];return v.length?v[v.length-1]:null;};
    return(
      <View>
        {view==='week'&&<View style={{marginHorizontal:16,marginBottom:10,backgroundColor:C.card,borderRadius:16,padding:16}}>
          <View style={{flexDirection:'row',justifyContent:'space-between',alignItems:'center',marginBottom:10}}>
            <Text style={{color:C.text,fontSize:15,fontWeight:'700'}}>{hasBaseline?`${personName}'s baseline is set`:`Learning ${personName}'s normal`}</Text>
            <Text style={{color:hasBaseline?C.green:C.orange,fontSize:13,fontWeight:'600'}}>{hasBaseline?'Active':`Day ${daysCollected} of ${daysNeeded}`}</Text>
          </View>
          <View style={{height:6,backgroundColor:C.fill,borderRadius:3}}>
            <View style={{height:6,width:`${progressPct}%` as any,backgroundColor:C.green,borderRadius:3}}/>
          </View>
        </View>}
        {view==='week'?(
          <>
          {wk.length===0?(
            <View style={{alignItems:'center',paddingVertical:36}}>
              <Text style={{color:C.sub,fontSize:15}}>No walking recorded this week</Text>
              <Text style={{color:C.muted,fontSize:12,marginTop:4}}>Data appears after {personName} is seen walking by the camera</Text>
            </View>
          ):(
            <>
              <Text style={s.secHeader}>Gait & Posture — This Week</Text>
              <PTrendBar data={wk.map(t=>t.avg_speed)} label="Walking Speed" unit="m/s" color={C.teal} dec={2}/>
              <PTrendBar data={wk.map(t=>t.avg_cadence)} label="Cadence" unit="spm" color={C.blue} dec={0}/>
              <PTrendBar data={wk.map(t=>t.avg_posture)} label="Posture Score" unit="/100" color={C.purple} dec={0}/>
              <PTrendBar data={wk.map(t=>t.avg_asymmetry)} label="Gait Asymmetry" unit="%" color={C.orange} dec={1}/>
            </>
          )}
          {watchWk.length>0&&(
            <>
              <Text style={s.secHeader}>{personName}'s Apple Watch — This Week</Text>
              <PTrendBar data={watchWk.map(r=>r.step_count)} label="Steps" unit="steps" color={C.green} dec={0}/>
              <PTrendBar data={watchWk.map(r=>r.resting_hr)} label="Resting HR" unit="bpm" color={C.red} dec={0}/>
              <PTrendBar data={watchWk.map(r=>r.hrv_sdnn)} label="HRV" unit="ms" color={C.purple} dec={1}/>
              <PTrendBar data={watchWk.map(r=>r.sleep_hours)} label="Sleep" unit="h" color={C.orange} dec={1}/>
            </>
          )}
          </>
        ):(
          <>
            {(()=>{
              const ser=(k:'avg_speed'|'avg_cadence'|'avg_posture'|'avg_asymmetry')=>trends.map(t=>({date:t.date,v:t[k]}));
              const wser=(k:'resting_hr'|'hrv_sdnn'|'sleep_hours')=>watchAll.map(r=>{const v=r[k];return{date:r.date,v:(typeof v==='number'&&isFinite(v)&&v>0)?v:null};});
              const hasWatch=watchAll.some(r=>r.resting_hr!=null||r.hrv_sdnn!=null||r.sleep_hours!=null);
              return <BaselineView name={personName} measures={[
                {key:'speed',label:'Walking speed',unit:'m/s',dec:2,why:BL_WHY.speed,series:ser('avg_speed')},
                {key:'cadence',label:'Steps per minute',unit:'steps/min',dec:0,why:BL_WHY.cadence,series:ser('avg_cadence')},
                {key:'posture',label:'Posture',unit:'/100',dec:0,why:BL_WHY.posture,series:ser('avg_posture')},
                {key:'asym',label:'Step evenness',unit:'% uneven',dec:1,higherWorse:true,why:BL_WHY.asym,series:ser('avg_asymmetry')},
                ...(hasWatch?[
                  {key:'rhr',label:'Resting heart rate',unit:'bpm',dec:0,higherWorse:true,why:BL_WHY.rhr,series:wser('resting_hr')},
                  {key:'hrv',label:'Heart rate variability',unit:'ms',dec:0,why:BL_WHY.hrv,series:wser('hrv_sdnn')},
                  {key:'sleep',label:'Sleep',unit:'h',dec:1,why:BL_WHY.sleep,series:wser('sleep_hours')},
                ]:[]),
              ]} groups={hasWatch?[{title:'Walking',keys:['speed','cadence','posture','asym']},
                                   {title:`${personName}'s Apple Watch`,keys:['rhr','hrv','sleep']}]:undefined}/>;
            })()}
          </>
        )}
      </View>
    );
  }

  return(
    <View style={{flex:1,backgroundColor:C.bg}}>
      <SafeAreaView style={{flex:1}}>
        <StatusBar barStyle="light-content"/>
        <View style={{flexDirection:'row',alignItems:'center',justifyContent:'space-between',paddingHorizontal:20,paddingTop:12,paddingBottom:8}}>
          <View>
            <Text style={{color:C.text,fontSize:28,fontWeight:'800'}}>{personName}</Text>
            <Text style={{color:C.sub,fontSize:13,marginTop:2}}>Individual trend history</Text>
          </View>
          <TouchableOpacity onPress={onClose} style={{backgroundColor:C.card,borderRadius:18,width:36,height:36,alignItems:'center',justifyContent:'center'}}>
            <Text style={{color:C.text,fontSize:16}}>Close</Text>
          </TouchableOpacity>
        </View>
        {pdLoading?(
          <View style={{flex:1,alignItems:'center',justifyContent:'center'}}><ActivityIndicator color={C.green} size="large"/></View>
        ):(
          <ScrollView showsVerticalScrollIndicator={false} automaticallyAdjustKeyboardInsets keyboardDismissMode="interactive" keyboardShouldPersistTaps="handled">
            <View style={{marginHorizontal:16,marginTop:8,marginBottom:8,backgroundColor:C.card,borderRadius:16,padding:16}}>
              <View style={{flexDirection:'row',justifyContent:'space-between',alignItems:'center',marginBottom:10}}>
                <Text style={{color:C.text,fontSize:15,fontWeight:'700'}}>{hasBaseline?'Baseline Established':'Building Baseline'}</Text>
                <Text style={{color:hasBaseline?C.green:C.orange,fontSize:13,fontWeight:'600'}}>{hasBaseline?'Active':`Day ${daysCollected} of ${daysNeeded}`}</Text>
              </View>
              <View style={{height:6,backgroundColor:C.fill,borderRadius:3}}>
                <View style={{height:6,width:`${progressPct}%` as any,backgroundColor:C.green,borderRadius:3}}/>
              </View>
              <Text style={{color:C.sub,fontSize:12,marginTop:10,lineHeight:18}}>
                {hasBaseline
                  ? `VIGIL has learned ${personName}'s normal walking speed, posture, and cadence from ${daysCollected} days of data. Today's readings are compared against their personal baseline.`
                  : `VIGIL is observing ${personName}'s normal patterns. After ${daysNeeded} days of data, anomaly detection compares future readings against their own baseline.`}
              </Text>
            </View>
            {/* Week-over-week comparison card */}
            {weeklyTrend?.changes && (()=>{
              const c = weeklyTrend.changes;
              const items = [
                {label:'Walking Speed', val:c.gait_speed_pct},
                {label:'Cadence',       val:c.cadence_pct},
                {label:'Posture',       val:c.posture_pct},
              ].filter(x=>x.val!=null);
              if(!items.length) return null;
              return(
                <View style={{marginHorizontal:16,marginBottom:8,backgroundColor:C.card,borderRadius:16,padding:16}}>
                  <Text style={{color:C.text,fontSize:15,fontWeight:'700',marginBottom:12}}>vs. Last Week</Text>
                  {items.map(item=>{
                    const up=item.val>0;
                    const neutral=Math.abs(item.val)<3;
                    const color=neutral?C.sub:up?C.green:C.red;
                    return(
                      <View key={item.label} style={{flexDirection:'row',justifyContent:'space-between',alignItems:'center',marginBottom:8}}>
                        <Text style={{color:C.sub,fontSize:13}}>{item.label}</Text>
                        <Text style={{color,fontSize:14,fontWeight:'700'}}>{up?'+':''}{item.val.toFixed(1)}%</Text>
                      </View>
                    );
                  })}
                </View>
              );
            })()}

            {trends.length===0?(
              <View style={{alignItems:'center',paddingVertical:48}}>
                <Text style={{color:C.sub,fontSize:15,fontWeight:'500'}}>No trend data yet</Text>
                <Text style={{color:C.muted,fontSize:12,marginTop:4}}>Data appears after {personName} is seen walking by the camera</Text>
              </View>
            ):(
              <>
                <Text style={s.secHeader}>Gait & Posture — Last 14 Days</Text>
                <PTrendBar data={trends.map(t=>t.avg_speed)} label="Walking Speed" unit="m/s" color={C.teal} dec={2}/>
                <PTrendBar data={trends.map(t=>t.avg_cadence)} label="Cadence" unit="spm" color={C.blue} dec={0}/>
                <PTrendBar data={trends.map(t=>t.avg_posture)} label="Posture Score" unit="/100" color={C.purple} dec={0}/>
                <PTrendBar data={trends.map(t=>t.avg_asymmetry)} label="Gait Asymmetry" unit="%" color={C.orange} dec={1}/>
              </>
            )}
            <View style={{height:40}}/>
          </ScrollView>
        )}
      </SafeAreaView>
    </View>
  );
}

// ─────────────────────────────────────────────
// PERSON PHOTO CARD — shows captured snapshot in identification modal
// ─────────────────────────────────────────────
// FALL_REPLAY_PLAYER: stick-figure replay of the 30 s before a fall.
// Coordinates only; no video is ever stored.
type ReplayFrame = [number, number[][], number[]];
const RP_EDGES:[number,number][] = [[5,6],[5,7],[7,9],[6,8],[8,10],[5,11],[6,12],[11,12],[11,13],[13,15],[12,14],[14,16],[0,5],[0,6]];
function demoReplay():ReplayFrame[]{
  // Sample data for Demo Mode: standing, a fall to the side, lying still.
  const stand=[[.50,.20],[.49,.19],[.51,.19],[.48,.20],[.52,.20],[.46,.29],[.54,.29],[.45,.39],[.55,.39],[.45,.48],[.55,.48],[.47,.50],[.53,.50],[.47,.65],[.53,.65],[.47,.80],[.53,.80]];
  const lie=[[.30,.78],[.29,.77],[.29,.79],[.30,.76],[.30,.80],[.37,.76],[.37,.81],[.45,.75],[.45,.82],[.52,.75],[.52,.82],[.55,.77],[.55,.80],[.67,.77],[.67,.80],[.79,.78],[.79,.79]];
  const out:ReplayFrame[]=[];
  for(let i=0;i<100;i++){
    const t=i*0.2;
    let f=0;
    if(i>=40&&i<45)f=(i-40)/4; else if(i>=45)f=1;
    const sway=i<40?Math.sin(i/4)*0.004:0;
    const pts=stand.map((p,k)=>[p[0]+(lie[k][0]-p[0])*f+sway,p[1]+(lie[k][1]-p[1])*f]);
    out.push([t,pts,stand.map(()=>0.9)]);
  }
  return out;
}
function FallReplayPlayer({frames:allFrames}:{frames:ReplayFrame[]}){
  // Only the 30 s before the alert, in time order. Older replays were saved
  // with stale samples from long before the fall.
  const frames=React.useMemo(()=>{
    const f=[...allFrames].sort((a,b)=>a[0]-b[0]);
    if(!f.length)return f;
    const end=f[f.length-1][0];
    return f.filter(x=>end-x[0]<=30);
  },[allFrames]);
  const [i,setI]=useState(0);
  useEffect(()=>{
    if(!frames.length)return;
    const id=setInterval(()=>setI(v=>(v+1)%frames.length),200);
    return()=>clearInterval(id);
  },[frames]);
  const W=SW-48, H=Math.round(W*9/16);
  const f=frames[Math.min(i,frames.length-1)];
  if(!f)return null;
  const [pts,conf]=[f[1],f[2]];
  const ok=(k:number)=>(conf[k]??0)>=0.3;
  const secsBeforeEnd=Math.max(0,(frames[frames.length-1][0]-f[0])).toFixed(1);
  return(
    <View>
      <View style={{width:W,height:H,backgroundColor:'#181818',borderRadius:14,overflow:'hidden'}}>
        <Svg width={W} height={H}>
          {RP_EDGES.map(([a,b],n)=>ok(a)&&ok(b)?(
            <Line key={n} x1={pts[a][0]*W} y1={pts[a][1]*H} x2={pts[b][0]*W} y2={pts[b][1]*H}
              stroke={C.green} strokeWidth={4} strokeLinecap="round"/>
          ):null)}
          {ok(0)&&<Circle cx={pts[0][0]*W} cy={pts[0][1]*H} r={Math.max(6,W*0.018)} stroke={C.green} strokeWidth={3} fill="none"/>}
        </Svg>
      </View>
      <View style={{height:4,backgroundColor:C.fill,borderRadius:2,marginTop:10,overflow:'hidden'}}>
        <View style={{width:`${(i/Math.max(1,frames.length-1))*100}%`,height:4,backgroundColor:C.red}}/>
      </View>
      <Text style={{color:C.muted,fontSize:11,marginTop:6}}>{`${secsBeforeEnd} s before the alert · plays on a loop`}</Text>
    </View>
  );
}

function PersonPhotoCard({personId, serverUrl, large=false}:{personId:string; serverUrl:string; large?:boolean}) {
  // Each person has exactly one stored photo. Until the camera has taken it,
  // keep checking every few seconds instead of showing "?" forever.
  const [photo, setPhoto] = useState<string|null>(null);
  useEffect(()=>{
    let alive=true; let tries=0;
    const load=()=>{
      AX.get(`${serverUrl||FALLBACK_URL}/persons/${personId}/photo`,{timeout:5000})
        .then(r=>{
          if(!alive)return;
          if(r.data?.available&&r.data?.photo_b64){setPhoto(r.data.photo_b64);return;}
          if(++tries<40)setTimeout(load,3000);
        })
        .catch(()=>{if(alive&&++tries<40)setTimeout(load,3000);});
    };
    load();
    return()=>{alive=false;};
  },[personId,serverUrl]);
  const W=large?160:96, H=large?160:72;
  if(!photo) return (
    <View style={{width:W,height:H,borderRadius:12,backgroundColor:C.fill,alignItems:'center',justifyContent:'center',marginBottom:10,alignSelf:large?'center':'flex-start'}}>
      <Text style={{color:C.muted,fontSize:large?13:24,textAlign:'center'}}>{large?'Taking photo...':'?'}</Text>
    </View>
  );
  return (
    <Image source={{uri:`data:image/jpeg;base64,${photo}`}}
      style={{width:W,height:H,borderRadius:12,marginBottom:10,alignSelf:large?'center':'flex-start'}}
      resizeMode="cover"/>
  );
}

// PEOPLE_UI_V1: photo avatars and a person picker, shared by every screen.
const PHOTO_CACHE:{[k:string]:string} = {};
// PHOTO_PICK_V1: avatars reload when a caregiver changes or removes a photo.
const PHOTO_SUBS = new Set<()=>void>();
function bumpPhoto(personId:string){ delete PHOTO_CACHE[personId]; PHOTO_SUBS.forEach(f=>f()); }
function initialsOf(name:string|null|undefined){
  const parts=(name??'').trim().split(/\s+/).filter(Boolean);
  if(!parts.length) return '?';
  return (parts[0].charAt(0)+(parts.length>1?parts[parts.length-1].charAt(0):'')).toUpperCase();
}
function PersonAvatar({personId,name,serverUrl,size=44,isResident=false}:
  {personId:string; name:string|null|undefined; serverUrl:string; size?:number; isResident?:boolean}) {
  const [photo,setPhoto] = useState<string|null>(PHOTO_CACHE[personId]??null);
  const [ver,setVer] = useState(0);
  useEffect(()=>{ const f=()=>setVer(v=>v+1); PHOTO_SUBS.add(f); return ()=>{ PHOTO_SUBS.delete(f); }; },[]);
  useEffect(()=>{
    if(PHOTO_CACHE[personId]){ setPhoto(PHOTO_CACHE[personId]); return; }
    let alive = true;
    AX.get(`${serverUrl||FALLBACK_URL}/persons/${personId}/photo`,{timeout:5000})
      .then(r=>{
        if(!alive) return;
        if(r.data?.available && r.data?.photo_b64){
          PHOTO_CACHE[personId] = r.data.photo_b64;
          setPhoto(r.data.photo_b64);
        } else { setPhoto(null); }
      })
      .catch(()=>{});
    return ()=>{ alive = false; };
  },[personId,serverUrl,ver]);
  const tint = isResident ? C.green : C.blue;
  if(photo) return (
    <Image source={{uri:`data:image/jpeg;base64,${photo}`}}
      style={{width:size,height:size,borderRadius:size/2,borderWidth:2,borderColor:tint}} resizeMode="cover"/>
  );
  const ini = initialsOf(name);
  return (
    <View style={{width:size,height:size,borderRadius:size/2,backgroundColor:tint+'26',alignItems:'center',justifyContent:'center',borderWidth:2,borderColor:tint+'66'}}>
      <Text style={{color:tint,fontWeight:'800',fontSize:Math.round(size*(ini.length>1?0.36:0.42)),letterSpacing:0.5}}>{ini}</Text>
    </View>
  );
}

function PersonChips({people,selectedId,onPick,serverUrl,allLabel}:
  {people:Person[]; selectedId:string|null; onPick:(p:Person|null)=>void; serverUrl:string; allLabel?:string}) {
  if(people.length===0) return null;
  return (
    <ScrollView horizontal showsHorizontalScrollIndicator={false}
      contentContainerStyle={{paddingHorizontal:16,gap:10,paddingBottom:10}}>
      {!!allLabel && (
        <TouchableOpacity onPress={()=>onPick(null)}
          style={{paddingHorizontal:14,paddingVertical:10,borderRadius:22,borderWidth:1.5,
                  borderColor:selectedId===null?C.green:C.border,backgroundColor:selectedId===null?C.green+'18':C.card,
                  justifyContent:'center'}}>
          <Text style={{color:selectedId===null?C.green:C.sub,fontWeight:'700',fontSize:13}}>{allLabel}</Text>
        </TouchableOpacity>
      )}
      {people.map(p=>{
        const on = selectedId===p.id;
        return (
          <TouchableOpacity key={p.id} onPress={()=>onPick(p)}
            style={{flexDirection:'row',alignItems:'center',gap:8,paddingLeft:6,paddingRight:14,paddingVertical:6,
                    borderRadius:26,borderWidth:1.5,borderColor:on?C.green:C.border,backgroundColor:on?C.green+'18':C.card}}>
            <PersonAvatar personId={p.id} name={p.name} serverUrl={serverUrl} size={34} isResident={!!p.is_resident}/>
            <View>
              <Text style={{color:C.text,fontWeight:'700',fontSize:13}} numberOfLines={1}>{p.name}</Text>
              <Text style={{color:C.muted,fontSize:10}}>{p.is_resident?'Resident':'Visitor'}</Text>
            </View>
          </TouchableOpacity>
        );
      })}
    </ScrollView>
  );
}

interface InsightItem {
  metric: string; label: string; unit: string;
  current: number; baseline_mean: number; baseline_std: number|null;
  baseline_days: number; delta_pct: number; z_score: number|null;
  direction: 'up'|'down'; adverse: boolean;
  severity: 'normal'|'watch'|'attention'; note: string;
}
interface ResidentInsights {
  person_id: string; name: string;
  status: 'building'|'insufficient_recent'|'ready';
  days_collected: number; days_needed: number;
  recent_days: number; recent_samples: number;
  insights: InsightItem[];
}

// Plain-language help, so a caregiver never needs to know what a baseline
// or a standard deviation is to understand what changed.
const METRIC_INFO:{[k:string]:{what:string;up:string;down:string}} = {
  avg_speed:{what:"How fast they walk. Walking slower than usual can come with tiredness, pain, weakness or illness.",up:"faster",down:"slower"},
  avg_cadence:{what:"Steps per minute. Fewer steps than usual often means more cautious or tired walking.",up:"more steps per minute",down:"fewer steps per minute"},
  avg_posture:{what:"How upright they stand and walk, scored 0 to 100. Lower means more leaning or slouching, which can come with fatigue, pain or balance problems.",up:"more upright",down:"less upright"},
  avg_asymmetry:{what:"How differently the left and right steps move. Higher means more uneven walking, which can come with pain or weakness on one side.",up:"more uneven",down:"more even"},
  resting_hr:{what:"Heart rate while resting, from Apple Watch. Higher than usual can come with illness, stress, dehydration or poor sleep.",up:"higher",down:"lower"},
  hrv_sdnn:{what:"Heart rate variability, from Apple Watch. Lower than usual is a general sign the body is under strain.",up:"higher",down:"lower"},
  spo2_avg:{what:"Blood oxygen, from Apple Watch. A sustained drop can mean breathing problems and is worth checking.",up:"higher",down:"lower"},
  respiratory_rate:{what:"Breaths per minute during sleep. Higher than usual can be an early sign of illness.",up:"higher",down:"lower"},
  wrist_temp:{what:"Skin temperature at night. Higher than usual can come with fever.",up:"higher",down:"lower"},
  sleep_hours:{what:"Hours of sleep. Less than usual can come with pain, worry or illness.",up:"more",down:"less"},
  step_count:{what:"Daily steps. Fewer than usual can mean they are less active or unwell.",up:"more",down:"fewer"},
  activity_level_pct:{what:"Share of the day spent moving, measured by the camera.",up:"more",down:"less"},
  longest_stillness_min:{what:"The longest stretch without moving while in view of the camera.",up:"longer",down:"shorter"},
};

function metricWhat(key:string):string{
  return METRIC_INFO[key]?.what ?? '';
}

function fmtMetric(metric:string,v:any):string{
  const n=Number(v);
  if(!isFinite(n)) return String(v);
  const dec:{[k:string]:number}={avg_speed:2,avg_cadence:0,avg_posture:0,avg_asymmetry:1};
  const d=dec[metric];
  return d===undefined ? String(Math.round(n*100)/100) : n.toFixed(d);
}

function plainChange(metric:string,pct:any,dir:string,base:any,unit:string):string{
  base = fmtMetric(metric, base);
  const p = Math.abs(Math.round(Number(pct)||0));
  if(p<3) return `About the same as usual (${base} ${unit})`;
  const info = METRIC_INFO[metric];
  const word = info ? (dir==='up'?info.up:info.down) : (dir==='up'?'higher':'lower');
  return `${p}% ${word} than usual (usually ${base} ${unit})`;
}

function plainRange(z:any):string{
  if(z===null||z===undefined) return 'Not enough history yet to say how unusual this is';
  const a = Math.abs(Number(z));
  if(a<1.5) return 'Within their normal day-to-day range';
  if(a<2) return 'Outside their usual range';
  return 'Well outside their usual range';
}

function AIInsightsScreen({serverUrl,demoMode,people=[]}:{serverUrl:string;demoMode?:boolean;people?:Person[]}) {
  const [residents,setResidents] = useState<ResidentInsights[]>([]);
  const [pick,setPick] = useState<string|null|undefined>(undefined);   // undefined = not chosen yet
  const named = people.filter(p=>!!p.name);
  const selId = pick===undefined ? (named.find(p=>!!p.is_resident)?.id ?? null) : pick;
  const [disclaimer,setDisclaimer] = useState<string>('');
  const [genAt,setGenAt]     = useState<string|null>(null);
  const [loading,setLoading] = useState(true);
  const [refreshing,setRefreshing] = useState(false);
  const [error,setError]     = useState<string|null>(null);
  const [face,setFace]       = useState<any>(null);
  const [sig,setSig]         = useState<any>(null);
  const [rout,setRout]       = useState<any>(null);
  const [pat,setPat]         = useState<any>(null);
  const [week,setWeek]       = useState<any>(null);

  const load = useCallback(async(quiet?:boolean)=>{
    if(demoMode){
      setResidents(DEMO_INSIGHTS.residents as any);
      setDisclaimer(DEMO_INSIGHTS.disclaimer);
      setGenAt(DEMO_INSIGHTS.generated_at);
      setSig(DEMO_SIGNALS); setFace(DEMO_FACE); setRout(DEMO_ROUTINE); setPat(DEMO_PATTERNS); setWeek(DEMO_WEEK);
      setLoading(false); setRefreshing(false); setError(null);
      return;
    }
    if(!quiet) setLoading(true);
    setError(null);
    try{
      const r = await AX.get(`${serverUrl||FALLBACK_URL}/insights/latest`,{timeout:8000});
      setResidents(r.data?.residents ?? []);
      setDisclaimer(r.data?.disclaimer ?? '');
      setGenAt(r.data?.generated_at ?? null);
      try{
        const f = await AX.get(`${serverUrl||FALLBACK_URL}/face/latest`,{timeout:6000});
        setFace(f.data ?? null);
      }catch(_e){ setFace(null); }   // optional: absence must not break insights
      try{
        const s = await AX.get(`${serverUrl||FALLBACK_URL}/signals/latest`,{timeout:6000});
        setSig(s.data ?? null);
      }catch(_e){ setSig(null); }
      try{
        const rt = await AX.get(`${serverUrl||FALLBACK_URL}/routine/latest`,{timeout:6000});
        setRout(rt.data ?? null);
      }catch(_e){ setRout(null); }
      try{
        const pt = await AX.get(`${serverUrl||FALLBACK_URL}/patterns/latest`,{timeout:6000});
        setPat(pt.data ?? null);
      }catch(_e){ setPat(null); }
      try{
        const wk = await AX.get(`${serverUrl||FALLBACK_URL}/summary/weekly`,{timeout:6000});
        setWeek(wk.data ?? null);
      }catch(_e){ setWeek(null); }
    }catch(e:any){ setError(e?.message ?? 'Request failed'); }
    finally{ setLoading(false); setRefreshing(false); }
  },[serverUrl,demoMode]);

  useEffect(()=>{load();},[load]);
  const onRefresh = useCallback(()=>{ setRefreshing(true); load(true); },[load]);

  const sevColor = (s:string)=> s==='attention'?C.red : s==='watch'?C.orange : C.green;
  const sevLabel = (s:string)=> s==='attention'?'ATTENTION' : s==='watch'?'WATCH' : 'NORMAL';

  if(loading) return (
    <View style={{flex:1,alignItems:'center',justifyContent:'center',backgroundColor:C.bg}}>
      <ActivityIndicator color={C.green} size="large"/>
      <Text style={{color:C.sub,marginTop:12}}>Loading insights...</Text>
    </View>
  );

  if(error) return (
    <View style={{flex:1,alignItems:'center',justifyContent:'center',backgroundColor:C.bg,padding:32}}>
      <Text style={{color:C.text,fontSize:18,fontWeight:'700',marginBottom:8}}>Cannot reach server</Text>
      <Text style={{color:C.sub,fontSize:13,textAlign:'center',marginBottom:16}}>{error}</Text>
      <TouchableOpacity style={{backgroundColor:C.blue,paddingHorizontal:24,paddingVertical:12,borderRadius:12}} onPress={()=>load()}>
        <Text style={{color:'#fff',fontWeight:'700',fontSize:14}}>Retry</Text>
      </TouchableOpacity>
    </View>
  );

  return (
    <ScrollView
      style={{flex:1,backgroundColor:C.bg}}
      showsVerticalScrollIndicator={false}
      refreshControl={<RefreshControl refreshing={refreshing} onRefresh={onRefresh} tintColor={C.green}/>}>

      <View style={{paddingHorizontal:20,paddingTop:20,paddingBottom:12}}>
        <Text style={{color:C.text,fontSize:34,fontWeight:'800'}}>AI Insights</Text>
        <Text style={{color:C.sub,fontSize:14,marginTop:2}}>
          Measured against each resident's own baseline
        </Text>
      </View>
      <PersonChips people={named} selectedId={selId} serverUrl={serverUrl} allLabel="Everyone"
        onPick={p=>setPick(p?p.id:null)}/>
      <Text style={{color:C.muted,fontSize:12,lineHeight:17,marginHorizontal:20,marginBottom:12}}>
        Health measures below belong to the person you pick. Routine, signals and the weekly summary further down describe the whole room.
      </Text>

      {residents.length===0 && (
        <View style={{alignItems:'center',padding:32}}>
          <Text style={{color:C.text,fontSize:16,fontWeight:'700'}}>No residents yet</Text>
          <Text style={{color:C.sub,fontSize:13,marginTop:6,textAlign:'center'}}>
            Identify a resident from the camera view to start building insights.
          </Text>
        </View>
      )}

      {residents.filter(r=>selId===null||r.person_id===selId).map(res=>{
        const attention = res.insights.filter(i=>i.severity==='attention').length;
        const watch     = res.insights.filter(i=>i.severity==='watch').length;
        const normal    = res.insights.filter(i=>i.severity==='normal').length;
        const pct = Math.max(0, Math.min(100, (res.days_collected/Math.max(1,res.days_needed))*100));

        return (
          <View key={res.person_id} style={{marginBottom:18}}>
            <View style={{flexDirection:'row',alignItems:'center',gap:12,marginHorizontal:20,marginBottom:10}}>
              <PersonAvatar personId={res.person_id} name={res.name} serverUrl={serverUrl} size={40}
                isResident={!!people.find(p=>p.id===res.person_id)?.is_resident}/>
              <View>
                <Text style={{color:C.text,fontSize:18,fontWeight:'700'}}>{res.name}</Text>
                <Text style={{color:C.muted,fontSize:11}}>
                  {people.find(p=>p.id===res.person_id)?.is_resident?'Resident':'Visitor'}
                </Text>
              </View>
            </View>

            {res.status==='building' && (
              <View style={{marginHorizontal:16,backgroundColor:C.card,borderRadius:16,padding:18}}>
                <Text style={{color:C.text,fontSize:15,fontWeight:'700'}}>Building baseline</Text>
                <Text style={{color:C.sub,fontSize:13,marginTop:6,lineHeight:19}}>
                  VIGIL learns {res.name}'s normal walking pattern before it can flag
                  meaningful change. {res.days_collected} of {res.days_needed} days collected.
                </Text>
                <View style={{height:8,backgroundColor:C.fill,borderRadius:4,marginTop:14,overflow:'hidden'}}>
                  <View style={{height:8,width:`${pct}%`,backgroundColor:C.green,borderRadius:4}}/>
                </View>
                <Text style={{color:C.muted,fontSize:11,marginTop:8}}>
                  {res.recent_samples} {res.recent_samples===1?'reading':'readings'} in the last {res.recent_days} days
                </Text>
              </View>
            )}

            {res.status==='insufficient_recent' && (
              <View style={{marginHorizontal:16,backgroundColor:C.card,borderRadius:16,padding:18}}>
                <Text style={{color:C.text,fontSize:15,fontWeight:'700'}}>Not enough recent activity</Text>
                <Text style={{color:C.sub,fontSize:13,marginTop:6,lineHeight:19}}>
                  A baseline exists, but only {res.recent_samples} readings were recorded in the
                  last {res.recent_days} days. Insights resume once {res.name} is seen walking again.
                </Text>
              </View>
            )}

            {res.status==='ready' && (
              <>
                <View style={{marginHorizontal:16,marginBottom:10,backgroundColor:C.card,borderRadius:16,padding:16,flexDirection:'row'}}>
                  {[['ATTENTION',attention,C.red],['WATCH',watch,C.orange],['NORMAL',normal,C.green]].map(([l,n,c])=>(
                    <View key={l as string} style={{flex:1,alignItems:'center'}}>
                      <Text style={{color:c as string,fontSize:28,fontWeight:'800'}}>{String(n)}</Text>
                      <Text style={{color:C.sub,fontSize:11,marginTop:2}}>{l as string}</Text>
                    </View>
                  ))}
                </View>

                {res.insights.map(it=>{
                  const col = sevColor(it.severity);
                  const arrow = it.direction==='up' ? '\u2191' : '\u2193';
                  return (
                    <View key={it.metric} style={{marginHorizontal:16,marginBottom:10,backgroundColor:C.card,
                                                  borderRadius:16,padding:16,borderLeftWidth:3,borderLeftColor:col}}>
                      <View style={{flexDirection:'row',justifyContent:'space-between',alignItems:'center'}}>
                        <Text style={{color:C.text,fontSize:15,fontWeight:'700',flex:1,marginRight:8}}>{it.label}</Text>
                        <View style={{backgroundColor:col+'22',borderColor:col+'55',borderWidth:1,
                                      paddingHorizontal:8,paddingVertical:3,borderRadius:8}}>
                          <Text style={{color:col,fontSize:10,fontWeight:'800',letterSpacing:0.5}}>
                            {sevLabel(it.severity)}
                          </Text>
                        </View>
                      </View>

                      <Text style={{color:C.text,fontSize:28,fontWeight:'800',marginTop:8,
                                    fontVariant:['tabular-nums']}}>
                        {fmtMetric(it.metric,it.current)}<Text style={{color:C.sub,fontSize:15,fontWeight:'600'}}> {it.unit}</Text>
                      </Text>

                      <Text style={{color:C.sub,fontSize:13,marginTop:4}}>
                        {arrow} {plainChange(it.metric,it.delta_pct,it.direction,it.baseline_mean,it.unit)}
                      </Text>
                      {it.z_score!==null && (
                        <Text style={{color:C.muted,fontSize:11,marginTop:3}}>
                          {plainRange(it.z_score)}
                          {it.baseline_days?` \u00b7 ${it.baseline_days}-day baseline`:''}
                        </Text>
                      )}

                      <View style={{height:1,backgroundColor:C.border,marginVertical:12}}/>
                      <Text style={{color:C.text,fontSize:12,lineHeight:18,marginBottom:6}}>{metricWhat(it.metric)}</Text>
                      <Text style={{color:C.sub,fontSize:12,lineHeight:18}}>{it.note}</Text>
                    </View>
                  );
                })}
              </>
            )}
          </View>
        );
      })}

      {!!week && Array.isArray(week.lines) && week.lines.length>0 && (
        <View style={{marginHorizontal:16,marginBottom:12,backgroundColor:C.card,borderRadius:16,padding:16}}>
          <View style={{flexDirection:'row',justifyContent:'space-between',alignItems:'center',marginBottom:10}}>
            <View style={{flex:1,marginRight:10}}>
              <Text style={{color:C.text,fontSize:15,fontWeight:'700'}}>This Week</Text>
              {!!week.period&&<Text style={{color:C.muted,fontSize:11,marginTop:2}}>{week.period}</Text>}
            </View>
            <TouchableOpacity onPress={()=>{Share.share({message:week.text}).catch(()=>{});}}
              style={{paddingHorizontal:12,paddingVertical:7,borderRadius:10,
                      borderWidth:1,borderColor:C.blue+'66'}}>
              <Text style={{color:C.blue,fontSize:12,fontWeight:'700'}}>Share with family</Text>
            </TouchableOpacity>
          </View>
          {week.lines.map((l:string,i:number)=>(
            <Text key={i} style={{color:C.sub,fontSize:13,lineHeight:20,marginBottom:4}}>{l}</Text>
          ))}
        </View>
      )}

      {!!pat && Array.isArray(pat.hourly) && pat.hourly.length===24 && (
        <View style={{marginHorizontal:16,marginBottom:12,backgroundColor:C.card,borderRadius:16,padding:16}}>
          <Text style={{color:C.text,fontSize:15,fontWeight:'700'}}>Activity Pattern</Text>
          <Text style={{color:C.sub,fontSize:12,marginTop:4,marginBottom:14}}>
            When {pat.days_of_history?`${pat.days_of_history} days of history show`:'this resident is'} activity, hour by hour
          </Text>

          {(()=>{
            const peak = Math.max(1, ...pat.hourly.map((h:any)=>Math.max(h.avg_minutes,h.today_minutes)));
            return (
              <View style={{flexDirection:'row',alignItems:'flex-end',height:84,gap:2}}>
                {pat.hourly.map((h:any)=>{
                  const night = h.hour>=22||h.hour<6;
                  const ah = Math.max(2,(h.avg_minutes/peak)*72);
                  const th = Math.max(0,(h.today_minutes/peak)*72);
                  return (
                    <View key={h.hour} style={{flex:1,alignItems:'center',justifyContent:'flex-end',height:84}}>
                      <View style={{width:'100%',height:ah,backgroundColor:night?C.blue+'44':C.fill,
                                    borderRadius:2,justifyContent:'flex-end'}}>
                        {th>0&&(
                          <View style={{width:'100%',height:Math.min(th,ah),
                                        backgroundColor:night?C.orange:C.green,borderRadius:2}}/>
                        )}
                      </View>
                    </View>
                  );
                })}
              </View>
            );
          })()}

          <View style={{flexDirection:'row',justifyContent:'space-between',marginTop:6}}>
            {['12a','6a','12p','6p','11p'].map(l=>(
              <Text key={l} style={{color:C.muted,fontSize:10}}>{l}</Text>
            ))}
          </View>

          <View style={{flexDirection:'row',gap:14,marginTop:10,alignItems:'center'}}>
            <View style={{flexDirection:'row',alignItems:'center',gap:5}}>
              <View style={{width:9,height:9,borderRadius:2,backgroundColor:C.fill}}/>
              <Text style={{color:C.muted,fontSize:11}}>usual</Text>
            </View>
            <View style={{flexDirection:'row',alignItems:'center',gap:5}}>
              <View style={{width:9,height:9,borderRadius:2,backgroundColor:C.green}}/>
              <Text style={{color:C.muted,fontSize:11}}>today</Text>
            </View>
            <View style={{flexDirection:'row',alignItems:'center',gap:5}}>
              <View style={{width:9,height:9,borderRadius:2,backgroundColor:C.blue+'44'}}/>
              <Text style={{color:C.muted,fontSize:11}}>night hours</Text>
            </View>
          </View>

          {pat.night_minutes_today>pat.night_minutes_avg*2&&pat.night_minutes_today>10&&(
            <View style={{marginTop:14,backgroundColor:C.orange+'18',borderRadius:10,padding:12}}>
              <Text style={{color:C.orange,fontSize:12,fontWeight:'700',marginBottom:3}}>
                More night movement than usual
              </Text>
              <Text style={{color:C.sub,fontSize:12,lineHeight:18}}>
                {Math.round(pat.night_minutes_today)} minutes active between 10pm and 6am,
                against a usual {Math.round(pat.night_minutes_avg)}. Falls often happen
                getting out of bed at night.
              </Text>
            </View>
          )}

          {Array.isArray(pat.anomalies)&&pat.anomalies.length>0&&(
            <View style={{marginTop:14}}>
              <Text style={{color:C.sub,fontSize:11,fontWeight:'700',letterSpacing:0.5,
                            textTransform:'uppercase',marginBottom:8}}>Unusual days</Text>
              {pat.anomalies.slice(0,4).map((a:any)=>(
                <View key={a.date} style={{flexDirection:'row',justifyContent:'space-between',
                                           paddingVertical:5}}>
                  <Text style={{color:C.text,fontSize:13}}>{a.date}</Text>
                  <Text style={{color:a.score>=90?C.orange:C.sub,fontSize:13,
                                fontWeight:'600',fontVariant:['tabular-nums']}}>
                    {a.score}
                  </Text>
                </View>
              ))}
              <Text style={{color:C.muted,fontSize:11,lineHeight:16,marginTop:8}}>
                {pat.method}
              </Text>
            </View>
          )}
        </View>
      )}

      {!!rout && Array.isArray(rout.items) && rout.items.length>0 && (
        <View style={{marginHorizontal:16,marginBottom:12,backgroundColor:C.card,borderRadius:16,padding:16}}>
          <Text style={{color:C.text,fontSize:15,fontWeight:'700',marginBottom:6}}>Daily Routine</Text>
          <Text style={{color:rout.flagged_count>=2?C.orange:C.sub,fontSize:13,lineHeight:19,marginBottom:14}}>
            {rout.summary}
          </Text>

          {rout.items.map((it:any)=>{
            if(it.status!=='ready'){
              return (
                <View key={it.label} style={{marginBottom:12,paddingBottom:12,
                                             borderBottomWidth:1,borderBottomColor:C.border}}>
                  <Text style={{color:C.muted,fontSize:14,fontWeight:'600'}}>{it.label}</Text>
                  <Text style={{color:C.muted,fontSize:11,marginTop:4}}>
                    Learning this pattern \u2014 {it.days_of_history} of {it.days_needed} days
                  </Text>
                </View>
              );
            }
            const low = it.stability==='low';
            const col = it.severity==='attention'?C.red:it.severity==='watch'?C.orange:C.sub;
            const shown = it.current_text ?? it.current;
            const base  = it.baseline_text ?? it.baseline;
            return (
              <View key={it.label} style={{marginBottom:12,paddingBottom:12,
                                           borderBottomWidth:1,borderBottomColor:C.border}}>
                <View style={{flexDirection:'row',justifyContent:'space-between',alignItems:'center'}}>
                  <Text style={{color:low?C.muted:C.text,fontSize:14,fontWeight:'600',flex:1,marginRight:8}}>
                    {it.label}
                  </Text>
                  {it.severity!=='normal'&&(
                    <View style={{backgroundColor:col+'22',borderColor:col+'55',borderWidth:1,
                                  paddingHorizontal:8,paddingVertical:2,borderRadius:7}}>
                      <Text style={{color:col,fontSize:9,fontWeight:'800',letterSpacing:0.5}}>
                        {it.severity==='attention'?'ATTENTION':'WATCH'}
                      </Text>
                    </View>
                  )}
                </View>

                <Text style={{color:low?C.muted:C.text,fontSize:20,fontWeight:'700',marginTop:4,
                              fontVariant:['tabular-nums']}}>
                  {shown}
                  {!it.current_text&&<Text style={{color:C.sub,fontSize:13,fontWeight:'600'}}> {it.unit}</Text>}
                </Text>

                <Text style={{color:C.sub,fontSize:12,marginTop:2}}>
                  usually {base}{it.current_text?'':` ${it.unit}`}
                  {it.days_of_history?` \u00b7 ${it.days_of_history} days`:''}
                </Text>

                {low && (
                  <Text style={{color:C.muted,fontSize:11,marginTop:4}}>
                    Varies too much day to day to flag reliably
                  </Text>
                )}
                {!low && it.severity!=='normal' && (
                  <Text style={{color:C.sub,fontSize:12,lineHeight:18,marginTop:8}}>{it.note}</Text>
                )}
              </View>
            );
          })}

          {!!rout.disclaimer && (
            <Text style={{color:C.muted,fontSize:11,lineHeight:16}}>{rout.disclaimer}</Text>
          )}
        </View>
      )}

      {!!sig && Array.isArray(sig.signals) && sig.signals.length>0 && (
        <View style={{marginHorizontal:16,marginBottom:12,backgroundColor:C.card,borderRadius:16,padding:16}}>
          <Text style={{color:C.text,fontSize:15,fontWeight:'700',marginBottom:6}}>Health Signals</Text>
          <Text style={{color:sig.flagged_count>=2?C.orange:C.sub,fontSize:13,lineHeight:19,marginBottom:14}}>
            {sig.summary}
          </Text>

          {sig.signals.map((s:any)=>{
            const low = s.baseline_stability==='low';
            const col = s.severity==='attention'?C.red:s.severity==='watch'?C.orange:C.sub;
            const arrow = s.direction==='up'?'\u2191':'\u2193';
            return (
              <View key={s.metric} style={{marginBottom:12,paddingBottom:12,
                                           borderBottomWidth:1,borderBottomColor:C.border}}>
                <View style={{flexDirection:'row',justifyContent:'space-between',alignItems:'center'}}>
                  <Text style={{color:low?C.muted:C.text,fontSize:14,fontWeight:'600',flex:1,marginRight:8}}>
                    {s.label}{s.who?<Text style={{color:C.muted,fontSize:12,fontWeight:'500'}}>{'  '}{s.who}</Text>:null}
                  </Text>
                  {s.severity!=='normal'&&(
                    <View style={{backgroundColor:col+'22',borderColor:col+'55',borderWidth:1,
                                  paddingHorizontal:8,paddingVertical:2,borderRadius:7}}>
                      <Text style={{color:col,fontSize:9,fontWeight:'800',letterSpacing:0.5}}>
                        {s.severity==='attention'?'ATTENTION':'WATCH'}
                      </Text>
                    </View>
                  )}
                </View>

                <Text style={{color:low?C.muted:C.text,fontSize:20,fontWeight:'700',marginTop:4,
                              fontVariant:['tabular-nums']}}>
                  {s.current}<Text style={{color:C.sub,fontSize:13,fontWeight:'600'}}> {s.unit}</Text>
                </Text>

                <Text style={{color:C.sub,fontSize:12,marginTop:2}}>
                  <Text style={{color:C.muted}}>{metricWhat(s.metric)}{'\n'}</Text>{arrow} {plainChange(s.metric,s.delta_pct,s.direction,s.baseline_mean,s.unit)}
                  {s.baseline_days?` \u00b7 ${s.baseline_days} days`:''}
                </Text>

                {low && (
                  <Text style={{color:C.muted,fontSize:11,marginTop:4}}>
                    Limited history — this measure varies too much day to day to flag reliably
                  </Text>
                )}
                {!low && s.severity!=='normal' && (
                  <Text style={{color:C.sub,fontSize:12,lineHeight:18,marginTop:8}}>{s.note}</Text>
                )}
              </View>
            );
          })}

          {!!sig.disclaimer && (
            <Text style={{color:C.muted,fontSize:11,lineHeight:16,marginTop:2}}>
              {sig.disclaimer}
            </Text>
          )}
        </View>
      )}

      {!!sig && sig.status!=='ready' && (
        <View style={{marginHorizontal:16,marginBottom:12,backgroundColor:C.card,borderRadius:16,padding:16}}>
          <Text style={{color:C.text,fontSize:15,fontWeight:'700'}}>Health Signals</Text>
          <Text style={{color:C.sub,fontSize:13,marginTop:6,lineHeight:19}}>
            Collecting daily history. Physiological measures need about 10 days
            before a personal norm can be established.
          </Text>
        </View>
      )}

      {!!face && (
        <View style={{marginHorizontal:16,marginBottom:12,backgroundColor:C.card,borderRadius:16,padding:16}}>
          <View style={{flexDirection:'row',justifyContent:'space-between',alignItems:'center',marginBottom:12}}>
            <Text style={{color:C.text,fontSize:15,fontWeight:'700'}}>Facial Cues</Text>
            <View style={{backgroundColor:C.blue+'22',borderColor:C.blue+'55',borderWidth:1,
                          paddingHorizontal:8,paddingVertical:3,borderRadius:8}}>
              <Text style={{color:C.blue,fontSize:10,fontWeight:'800',letterSpacing:0.5}}>EXPERIMENTAL</Text>
            </View>
          </View>

          <View style={{flexDirection:'row'}}>
            <View style={{flex:1}}>
              <Text style={{color:C.text,fontSize:28,fontWeight:'800',fontVariant:['tabular-nums']}}>
                {face.yawns_today ?? 0}
              </Text>
              <Text style={{color:C.sub,fontSize:12,marginTop:2}}>Yawns today</Text>
            </View>
            <View style={{flex:1}}>
              <Text style={{color:C.text,fontSize:28,fontWeight:'800',fontVariant:['tabular-nums']}}>
                {face.perclos_1h_avg!=null?`${Math.round(face.perclos_1h_avg*100)}%`:'--'}
              </Text>
              <Text style={{color:C.sub,fontSize:12,marginTop:2}}>Eyes closed, last hour</Text>
            </View>
          </View>

          <View style={{height:1,backgroundColor:C.border,marginVertical:12}}/>
          <Text style={{color:C.sub,fontSize:12,lineHeight:18}}>
            Yawn frequency and eye-closure time are established fatigue indicators.
            VIGIL is still collecting enough history to compare these against this
            resident's own norm, so treat single readings as observations rather
            than conclusions.
          </Text>
          {face.perclos_samples!=null&&(
            <Text style={{color:C.muted,fontSize:11,marginTop:8}}>
              {face.perclos_samples} eye-closure samples in the last hour
            </Text>
          )}
        </View>
      )}

      {!!disclaimer && (
        <View style={{marginHorizontal:16,marginBottom:12,backgroundColor:C.card,borderRadius:12,
                      borderWidth:1,borderColor:C.fill,padding:14}}>
          <Text style={{color:C.orange,fontSize:11,fontWeight:'700',letterSpacing:0.5,
                        marginBottom:6,textTransform:'uppercase'}}>Wellness Monitoring Only</Text>
          <Text style={{color:C.sub,fontSize:12,lineHeight:18}}>{disclaimer}</Text>
        </View>
      )}

      {genAt && (
        <Text style={{color:C.muted,fontSize:11,textAlign:'center',marginBottom:24}}>
          {'Updated '+timeAgo(genAt)}
        </Text>
      )}
      <View style={{height:24}}/>
    </ScrollView>
  );
}

// ─────────────────────────────────────────────
// MAIN APP
// ─────────────────────────────────────────────

// ─────────────────────────────────────────────
// DEMO MODE
// Apple requires a fully-featured demo mode when an app depends on
// hardware a reviewer does not have. Values below mirror this system's
// real measured output so the demo is representative, not invented.
// ─────────────────────────────────────────────
const DEMO_RESIDENT = 'Margaret (Demo Resident)';

function demoDate(back:number):string{
  const d=new Date(); d.setDate(d.getDate()-back);
  return d.toISOString().slice(0,10);
}
function demoIso(hoursAgo:number):string{
  return new Date(Date.now()-hoursAgo*3600*1000).toISOString();
}


// Person detail demo data. The final days drift downward on purpose so
// the detail screen shows the kind of change VIGIL exists to surface.
const DEMO_PTRENDS:PersonTrendDay[] = Array.from({length:14},(_,i):PersonTrendDay=>{
  const drift = i>=10 ? (i-9)*0.035 : 0;
  const w=(n:number,a:number)=>+(n+((i%3)-1)*a).toFixed(2);
  return {
    date: demoDate(13-i),
    avg_speed: +(w(1.14,0.04)-drift).toFixed(2),
    avg_cadence: +(w(95.7,2.6)-drift*40).toFixed(1),
    avg_posture: w(43.7,3.2),
    avg_asymmetry: +(w(4.6,0.6)+drift*8).toFixed(1),
    samples: Math.round(w(180,25)),
  };
});

const DEMO_PBASELINE:PersonBaselineStatus = {
  person_id:'demo_1', has_baseline:true, days_collected:14, days_needed:7,
  baseline:{
    avg_speed:{mean:1.131,std:0.06},
    avg_cadence:{mean:95.66,std:3.37},
    avg_posture:{mean:43.68,std:13.45},
    avg_asymmetry:{mean:4.62,std:0.71},
  },
};

const DEMO_WEEKLY = {
  this_week:{avg_speed:1.04,avg_cadence:91.8,avg_posture:42.1,avg_asymmetry:5.8},
  last_week:{avg_speed:1.15,avg_cadence:96.4,avg_posture:44.2,avg_asymmetry:4.5},
  change_pct:{avg_speed:-9.6,avg_cadence:-4.8,avg_posture:-4.8,avg_asymmetry:28.9},
};

const DEMO_TODAY:DailySummary = {
  date: demoDate(0), step_count: 3820, hrv_sdnn: 38.4, resting_hr: 74,
  sleep_hours: 6.4, active_calories: 210, spo2_avg: 96.8,
  walking_asymmetry_pct: 5.4, walking_speed_ms: 1.04,
  camera_gait_speed_ms: 1.01, camera_cadence_spm: 90.2,
  camera_asymmetry_pct: 6.1, posture_score: 41.8,
  active_minutes: 168, still_minutes: 430, activity_level_pct: 28.1,
  longest_stillness_min: 61, camera_data_points: 1840, anomaly_flag: 1,
};

const DEMO_HISTORY:DailySummary[] = Array.from({length:14},(_,i):DailySummary=>{
  const w=(n:number,a:number)=>+(n+((i%3)-1)*a).toFixed(2);
  return {
    date: demoDate(13-i),
    step_count: Math.round(w(4300,420)),
    hrv_sdnn: w(41,3.1), resting_hr: Math.round(w(68,2)),
    sleep_hours: w(7.1,0.5), active_calories: Math.round(w(240,30)),
    spo2_avg: w(97.2,0.4), walking_asymmetry_pct: w(3.8,0.6),
    walking_speed_ms: w(1.14,0.05), camera_gait_speed_ms: w(1.13,0.05),
    camera_cadence_spm: w(95.7,3.2), camera_asymmetry_pct: w(4.6,0.7),
    posture_score: w(43.7,4.0), active_minutes: Math.round(w(214,26)),
    still_minutes: Math.round(w(372,40)), activity_level_pct: w(36.5,5),
    longest_stillness_min: Math.round(w(42,9)),
    camera_data_points: Math.round(w(2100,300)), anomaly_flag: 0,
  };
});

const DEMO_ALERTS:AlertEntry[] = [
  { id:3, alert_type:'bed_exit', detected_at:demoIso(9), created_at:demoIso(9), seen:1,
    payload:{rested_min:142, hour:2,
             message:'Got up during the night after resting about 142 minutes'} },
  { id:2, alert_type:'fall', detected_at:demoIso(3), created_at:demoIso(3), seen:0,
    payload:{hip_drop_pct:1.0,torso_angle:78.4,aspect:3.08,
             message:'Fall detected by camera',resident_name:DEMO_RESIDENT} },
  { id:1, alert_type:'health_signal', detected_at:demoIso(26), created_at:demoIso(26), seen:1,
    payload:{metric:'avg_speed',
             message:'Walking speed 11% below 14-day baseline'} },
];

const DEMO_PERSONS:Person[] = [
  { id:'demo_1', name:DEMO_RESIDENT, room_id:'room_1', is_resident:1,
    height_px:742.3, confidence:0.92, frames_seen:18420,
    first_seen_at:demoIso(24*14), last_seen_at:demoIso(0.2) },
];

const DEMO_CAMERA:CameraStatus = {
  online:true, fps:11.6, person_present:true, person_count:1,
  last_seen_minutes_ago:0, camera_mode:'live',
};

const DEMO_ROOMS:Room[] = [
  { id:'room_1', name:'Room 1', resident_name:DEMO_RESIDENT,
    emergency_phone:'+1 (555) 010-0000', camera_online:true,
    person_present:true, person_count:1, fps:11.6 },
];

const DEMO_INSIGHTS = {
  generated_at:new Date().toISOString(),
  residents:[{
    person_id:'demo_1', name:DEMO_RESIDENT, status:'ready',
    days_collected:14, days_needed:7, recent_days:3, recent_samples:1620,
    insights:[
      { metric:'avg_speed', label:'Walking Speed', unit:'m/s', current:1.01,
        baseline_mean:1.131, baseline_std:0.06, baseline_days:14,
        delta_pct:-10.7, z_score:-2.02, direction:'down', adverse:true,
        severity:'attention',
        note:"Walking speed is a widely used functional measure in older adults. "+
             "A sustained decline relative to a person's own norm is associated "+
             "with increased fall risk." },
      { metric:'avg_cadence', label:'Cadence', unit:'steps/min', current:90.2,
        baseline_mean:95.66, baseline_std:3.37, baseline_days:14,
        delta_pct:-5.7, z_score:-1.62, direction:'down', adverse:true,
        severity:'watch',
        note:"Cadence below a person's own norm can accompany fatigue, pain, "+
             "or reduced walking confidence." },
      { metric:'avg_posture', label:'Posture Score', unit:'/100', current:41.8,
        baseline_mean:43.68, baseline_std:13.45, baseline_days:14,
        delta_pct:-4.3, z_score:-0.14, direction:'down', adverse:true,
        severity:'normal',
        note:"Lower scores reflect greater trunk lean or shoulder asymmetry." },
    ],
    convergence:{ adverse_count:3, total_metrics:3, attention:1, watch:1,
      summary:"Multiple signals have moved away from this person's normal at "+
              "the same time. Convergence across several measures is more "+
              "meaningful than any one reading." },
  }],
  disclaimer:"VIGIL reports measured changes against each resident's own "+
             "baseline. It does not diagnose conditions and is not a medical device.",
};

const DEMO_SIGNALS = {
  status:'ready', generated_at:new Date().toISOString(),
  baseline_days:21, recent_days:3, flagged_count:2,
  summary:'2 signals have moved away from normal at the same time. '+
          'Convergence across several measures is more meaningful than any '+
          'single reading.',
  signals:[
    { metric:'resting_hr', label:'Resting Heart Rate', unit:'bpm', current:74.0,
      baseline_mean:68.2, baseline_std:2.1, baseline_days:18, baseline_cv:0.03,
      baseline_stability:'ok', delta_pct:8.5, z_score:2.76, direction:'up',
      adverse:true, severity:'attention',
      note:"A resting heart rate above a person's own norm can accompany "+
           "infection, dehydration, pain or poor sleep. It often rises before "+
           "other symptoms appear." },
    { metric:'sleep_hours', label:'Sleep', unit:'hours', current:6.4,
      baseline_mean:7.14, baseline_std:0.42, baseline_days:18, baseline_cv:0.06,
      baseline_stability:'ok', delta_pct:-10.4, z_score:-1.76, direction:'down',
      adverse:true, severity:'watch',
      note:"Reduced sleep can accompany pain, anxiety, breathing difficulty "+
           "or illness." },
    { metric:'spo2_avg', label:'Blood Oxygen', unit:'%', current:96.8,
      baseline_mean:97.2, baseline_std:0.38, baseline_days:18, baseline_cv:0.00,
      baseline_stability:'ok', delta_pct:-0.4, z_score:-1.05, direction:'down',
      adverse:true, severity:'normal',
      note:"A sustained fall in blood oxygen saturation can indicate "+
           "respiratory compromise." },
  ],
  disclaimer:"These are deviations from this resident's own recent norm, not "+
             "diagnoses. VIGIL does not identify medical conditions and is not "+
             "a medical device.",
};

const DEMO_WEEK_LINES = [
  'Walking speed averaged 1.04 m/s, down 9% from last week.',
  'Cadence averaged 92 steps/min, down 4% from last week.',
  'About 3.1 hours active per day, down 12% from last week.',
  'Night-time activity: 6 episodes this week (2 last week).',
  'Alerts this week: 1 fall, 1 night bed exit.',
  'Median time to acknowledge an alert: 3 min.',
];
const DEMO_WEEK = {
  period:'Last 7 days', name:DEMO_RESIDENT, lines:DEMO_WEEK_LINES,
  text:'VIGIL weekly summary for '+DEMO_RESIDENT+' (last 7 days)\n\n'+
       DEMO_WEEK_LINES.join('\n')+
       '\n\nVIGIL compares each resident with their own history. It does not diagnose conditions.',
};

const DEMO_PATTERNS = {
  generated_at:new Date().toISOString(), days_of_history:21, days_scored:78,
  busiest_hours:[9,14,19], night_minutes_avg:4.2, night_minutes_today:31.5,
  hourly:[0,0,0,0,0,2,14,38,52,61,44,33,29,41,58,47,35,40,56,62,38,19,6,1]
    .map((v,h)=>({hour:h, avg_minutes:v,
                  today_minutes: h>=0&&h<6 ? (h===2?12:h===3?19:0) : Math.max(0,v-8),
                  days_with_activity: v>0?21:0})),
  anomalies:[{date:'2026-09-26',score:88.4},{date:'2026-09-19',score:74.1}],
  method:"Unusual days are identified by an isolation forest trained only on "+
         "this resident's own daily history.",
  disclaimer:"An unusual day is an observation, not a diagnosis. It indicates "+
             "the day differed from this resident's pattern, not why.",
};

const DEMO_ROUTINE = {
  status:'ready', generated_at:new Date().toISOString(), flagged_count:1,
  summary:'Night Activity is outside its usual range.',
  items:[
    { label:'Morning Start', status:'ready', unit:'minutes after midnight',
      current:498, baseline:432, spread:28, stability:'ok', spread_ratio:0.06,
      current_text:'08:18', baseline_text:'07:12', days_of_history:21,
      z_score:2.36, severity:'watch',
      note:'A later start than usual can reflect poor sleep, pain, low mood or illness. It is one of the earliest observable changes in daily routine.' },
    { label:'Night Activity', status:'ready', unit:'episodes',
      current:4, baseline:1, spread:1, stability:'ok', spread_ratio:1.0,
      days_of_history:21, z_score:3.0, severity:'attention',
      note:'Movement between 22:00 and 06:00. This matters for safety: many falls happen getting out of bed at night. More night waking than usual can also accompany a urinary issue, a medication side effect, or disturbed sleep.' },
    { label:'Night Time Active', status:'ready', unit:'minutes',
      current:38, baseline:12, spread:6, stability:'ok', spread_ratio:0.5,
      days_of_history:21, z_score:4.3, severity:'attention',
      note:'Total time moving overnight. Separates one long episode from several brief ones.' },
    { label:'Longest Daytime Absence', status:'ready', unit:'minutes',
      current:42, baseline:38, spread:14, stability:'ok', spread_ratio:0.37,
      days_of_history:21, z_score:0.29, severity:'normal',
      note:'Time away from the monitored room during the day.' },
  ],
  disclaimer:"Routine changes are observations about this resident's own pattern, not diagnoses.",
};

const DEMO_FACE = {
  yawns_today:4, perclos_1h_avg:0.118, perclos_1h_max:0.21,
  perclos_samples:54, recent:[],
  disclaimer:'Facial cues are wellness signals, not a diagnosis.',
};

// Who is responding. Asked once, saved on this phone, sent with every
// acknowledgement so the record shows a person, not just "someone".
async function caregiverName():Promise<string>{
  try{
    const saved = await AsyncStorage.getItem('vigil_caregiver_name');
    if(saved) return saved;
  }catch(_e){}
  return await new Promise<string>(resolve=>{
    Alert.prompt(
      'Who is responding?',
      'Your name is saved with acknowledgements so the record shows who checked on the resident.',
      [
        {text:'Skip',style:'cancel',onPress:()=>resolve('Caregiver')},
        {text:'Save',onPress:(v?:string)=>{
          const n=(v||'').trim()||'Caregiver';
          AsyncStorage.setItem('vigil_caregiver_name',n).catch(()=>{});
          resolve(n);
        }},
      ],
      'plain-text');
  });
}

// KEYBOARD_CALM_V1: a text field that keeps its own text. Typing redraws
// only this field; the app hears about it 0.2 s after typing pauses, and
// immediately on leaving the field or pressing Return.
const KInput=React.memo(function KInput({value,onChange,onSubmit,liveRef,style,...rest}:any){
  const [t,setT]=useState<string>(value??'');
  const focused=useRef(false);
  const timer=useRef<any>(null);
  const cb=useRef(onChange); cb.current=onChange;
  useEffect(()=>{ if(liveRef) liveRef.current=value??''; },[]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(()=>{ if(!focused.current) setT(value??''); },[value]);
  useEffect(()=>()=>{ if(timer.current) clearTimeout(timer.current); },[]);
  const flush=(v:string)=>{ if(timer.current){clearTimeout(timer.current);timer.current=null;} if(cb.current) cb.current(v); };
  return(
    <TextInput {...rest} style={style} value={t}
      onChangeText={(v:string)=>{
        setT(v); if(liveRef) liveRef.current=v;
        if(timer.current) clearTimeout(timer.current);
        timer.current=setTimeout(()=>flush(v),200);
      }}
      onFocus={()=>{focused.current=true;}}
      onBlur={()=>{focused.current=false; flush(t);}}
      onSubmitEditing={(e:any)=>{ const v=e?.nativeEvent?.text??t; flush(v); if(onSubmit) onSubmit(v); }}/>
  );
});

// Drawn tab-bar icons (match the website's line icons).
function TabIcon({name,color}:{name:string;color:string}){
  const p={stroke:color,strokeWidth:2,fill:'none',strokeLinecap:'round' as const,strokeLinejoin:'round' as const};
  return(
    <Svg width={24} height={24} viewBox="0 0 24 24">
      {name==='today'&&<Path d="M3 12h4l3-8 4 16 3-8h4" {...p}/>}
      {name==='trends'&&<><Path d="M3 17l6-6 4 4 8-8" {...p}/><Path d="M15 7h6v6" {...p}/></>}
      {name==='alerts'&&<><Path d="M6 8a6 6 0 0 1 12 0c0 7 3 9 3 9H3s3-2 3-9" {...p}/><Path d="M10.3 21a1.9 1.9 0 0 0 3.4 0" {...p}/></>}
      {name==='insights'&&<><Path d="M12 3l1.9 5.1L19 10l-5.1 1.9L12 17l-1.9-5.1L5 10l5.1-1.9z" {...p}/><Path d="M19 15.5l.7 1.8 1.8.7-1.8.7-.7 1.8-.7-1.8-1.8-.7 1.8-.7z" {...p}/></>}
      {name==='settings'&&<><Circle cx={12} cy={12} r={3} {...p}/><Path d="M12 2.8v2.4M12 18.8v2.4M2.8 12h2.4M18.8 12h2.4M5.5 5.5l1.7 1.7M16.8 16.8l1.7 1.7M5.5 18.5l1.7-1.7M16.8 7.2l1.7-1.7" {...p}/></>}
    </Svg>
  );
}

// ROOM_SETUP_V1: draw bed and couch zones on the live picture.
type Zone={x1:number;y1:number;x2:number;y2:number;label?:string};
function ZoneEditor({serverUrl,onClose}:{serverUrl:string;onClose:()=>void}){
  const W=SW-32, H=Math.round((SW-32)*9/16);
  const u=serverUrl||FALLBACK_URL;
  const [frame,setFrame]=useState<string|null>(null);
  const [zones,setZones]=useState<Zone[]>([]);
  const [draft,setDraft]=useState<Zone|null>(null);
  const [label,setLabel]=useState('Bed');
  const [msg,setMsg]=useState<string|null>(null);
  const [saving,setSaving]=useState(false);
  const start=useRef<{x:number;y:number}|null>(null);
  useEffect(()=>{
    AX.get(`${u}/camera/snapshot?w=800`,{timeout:6000})
      .then(r=>{if(r.data?.frame)setFrame(r.data.frame);else setMsg('The camera picture is not available right now.');})
      .catch(()=>setMsg('Could not reach the camera unit.'));
    AX.get(`${u}/zones`,{timeout:6000})
      .then(r=>{if(Array.isArray(r.data?.zones))setZones(r.data.zones);})
      .catch(()=>{});
  },[u]);
  const clamp=(v:number)=>Math.max(0,Math.min(1,v));
  const pr=useRef(PanResponder.create({
    onStartShouldSetPanResponder:()=>true,
    onMoveShouldSetPanResponder:()=>true,
    onPanResponderGrant:(e)=>{
      const {locationX,locationY}=e.nativeEvent;
      const p={x:clamp(locationX/W),y:clamp(locationY/H)};
      start.current=p; setDraft({x1:p.x,y1:p.y,x2:p.x,y2:p.y});
    },
    onPanResponderMove:(_e,g)=>{
      const p=start.current; if(!p)return;
      const x=clamp(p.x+g.dx/W), y=clamp(p.y+g.dy/H);
      setDraft({x1:Math.min(p.x,x),y1:Math.min(p.y,y),x2:Math.max(p.x,x),y2:Math.max(p.y,y)});
    },
    onPanResponderRelease:()=>{start.current=null;},
  })).current;
  const big=(z:Zone|null)=>!!z&&(z.x2-z.x1)>=0.03&&(z.y2-z.y1)>=0.03;
  const box=(z:Zone,key:string,col:string,dashed=false)=>(
    <View key={key} pointerEvents="none" style={{position:'absolute',left:z.x1*W,top:z.y1*H,width:(z.x2-z.x1)*W,height:(z.y2-z.y1)*H,
      borderWidth:2,borderColor:col,borderStyle:dashed?'dashed':'solid',backgroundColor:col+'33',borderRadius:6}}>
      {!!z.label&&<Text style={{color:'#fff',fontSize:11,fontWeight:'800',backgroundColor:col,alignSelf:'flex-start',paddingHorizontal:6,paddingVertical:2,borderBottomRightRadius:6}}>{z.label}</Text>}
    </View>
  );
  const save=async()=>{
    setSaving(true); setMsg(null);
    try{
      const r=await AX.post(`${u}/zones`,{zones},{timeout:8000});
      if(Array.isArray(r.data?.zones))setZones(r.data.zones);
      setMsg('Saved. VIGIL uses these zones within 5 seconds.');
    }catch(_e){ setMsg('Could not save. Check the connection to the camera unit.'); }
    finally{ setSaving(false); }
  };
  return(
    <SafeAreaView style={{flex:1,backgroundColor:C.bg}}>
      <View style={{flexDirection:'row',justifyContent:'space-between',alignItems:'center',paddingHorizontal:16,paddingVertical:12}}>
        <Text style={{color:C.text,fontSize:17,fontWeight:'800'}}>Room setup</Text>
        <TouchableOpacity onPress={onClose} hitSlop={{top:10,bottom:10,left:10,right:10}}><Text style={{color:C.green,fontSize:16,fontWeight:'700'}}>Done</Text></TouchableOpacity>
      </View>
      <Text style={{color:C.sub,fontSize:14,lineHeight:20,marginHorizontal:16,marginBottom:12}}>
        Drag a box over each bed, couch and armchair. Lying down inside a box is never treated as a fall, and night bed exits are watched from the bed.
      </Text>
      <View style={{marginHorizontal:16,width:W,height:H,borderRadius:14,overflow:'hidden',backgroundColor:C.card,borderWidth:1,borderColor:C.border}}>
        {frame
          ?<Image source={{uri:`data:image/jpeg;base64,${frame}`}} style={{width:W,height:H}} resizeMode="cover"/>
          :<View style={{flex:1,alignItems:'center',justifyContent:'center'}}><ActivityIndicator color={C.green}/></View>}
        <View style={{position:'absolute',left:0,top:0,width:W,height:H}} {...pr.panHandlers}>
          {zones.map((z,i)=>box(z,'z'+i,C.green))}
          {draft&&big(draft)&&box({...draft,label},'draft',C.orange,true)}
        </View>
      </View>
      <ScrollView contentContainerStyle={{padding:16,paddingBottom:40}}>
        <View style={{flexDirection:'row',gap:8,marginBottom:12}}>
          {['Bed','Couch','Chair'].map(l=>(
            <TouchableOpacity key={l} onPress={()=>setLabel(l)}
              style={{paddingHorizontal:16,paddingVertical:9,borderRadius:100,borderWidth:1.5,borderColor:label===l?C.green:C.border,backgroundColor:label===l?C.green+'22':'transparent'}}>
              <Text style={{color:label===l?C.green:C.sub,fontWeight:'700'}}>{l}</Text>
            </TouchableOpacity>
          ))}
        </View>
        <TouchableOpacity disabled={!big(draft)} onPress={()=>{if(draft&&big(draft)){setZones(z=>[...z,{...draft,label}].slice(0,6));setDraft(null);}}}
          style={[s.btnSecondary,{opacity:big(draft)?1:0.4}]}>
          <Text style={s.btnSecondaryTxt}>{big(draft)?`Add this box as ${label}`:'Drag on the picture to draw a box'}</Text>
        </TouchableOpacity>
        {zones.length>0&&(
          <View style={[s.settGrp,{marginHorizontal:0,marginTop:16}]}>
            {zones.map((z,i)=>(
              <View key={i} style={{flexDirection:'row',justifyContent:'space-between',alignItems:'center',paddingHorizontal:16,paddingVertical:13,borderTopWidth:i?1:0,borderTopColor:C.hair}}>
                <Text style={{color:C.text,fontSize:15}}>{z.label||'Furniture'}</Text>
                <TouchableOpacity onPress={()=>setZones(zs=>zs.filter((_x,j)=>j!==i))}><Text style={{color:C.red,fontWeight:'700'}}>Remove</Text></TouchableOpacity>
              </View>
            ))}
          </View>
        )}
        <TouchableOpacity onPress={save} disabled={saving} style={[s.btnPrimary,{marginTop:18,opacity:saving?0.6:1}]}>
          <Text style={s.btnPrimaryTxt}>{saving?'Saving...':'Save zones'}</Text>
        </TouchableOpacity>
        {!!msg&&<Text style={{color:C.sub,fontSize:13,textAlign:'center',marginTop:12}}>{msg}</Text>}
        <Text style={{color:C.muted,fontSize:12,lineHeight:18,marginTop:18}}>
          Keep the camera fixed, with automatic tracking turned off. If the camera is moved, redo this screen.
        </Text>
      </ScrollView>
    </SafeAreaView>
  );
}

// Defined outside App so its identity is stable across re-renders;
// inside App, every re-render remounted each text field and the
// keyboard flickered.
const OField=({label,value,onChange,placeholder,keyboard='default',note}:any)=>(
  <View style={{paddingHorizontal:16,paddingVertical:12,borderBottomWidth:1,borderBottomColor:C.hair}}>
    <View style={{flexDirection:'row',alignItems:'center',justifyContent:'space-between'}}>
      <Text style={{color:C.text,fontSize:15}}>{label}</Text>
      <KInput style={{color:C.text,fontSize:15,textAlign:'right',flex:1,marginLeft:16,paddingVertical:6}} value={value} onChange={onChange} placeholder={placeholder} placeholderTextColor={C.muted} keyboardType={keyboard} autoCapitalize="none" autoCorrect={false} returnKeyType="done"/>
    </View>
    {note&&<Text style={{color:C.muted,fontSize:11,marginTop:4}}>{note}</Text>}
  </View>
);

export default function App() {
  // KEYBOARD_CALM_V1: background polling pauses while the keyboard is up.
  const kbOpenRef=useRef(false);
  useEffect(()=>{
    const a=Keyboard.addListener('keyboardWillShow',()=>{kbOpenRef.current=true;});
    const b=Keyboard.addListener('keyboardDidHide',()=>{kbOpenRef.current=false;});
    return()=>{a.remove();b.remove();};
  },[]);

  // ── Boot state ──
  const [booting,setBooting]           = useState(true);
  const [showOnboarding,setOnboarding] = useState(false);
  const [pendingCode,setPendingCode]   = useState<string|null>(null);   // APP_SETUP_V1: vigil://setup?code=XXXXXXXX
  useEffect(()=>{
    const take=(url:string|null)=>{
      const m=(url||'').match(/^vigil:\/\/setup\?(?:.*&)?code=([A-Za-z0-9-]{8,9})/);
      if(!m)return;
      const c=m[1].replace(/-/g,'').toUpperCase();
      setPendingCode(c);AsyncStorage.setItem('vigil_pending_code',c).catch(()=>{});
    };
    Linking.getInitialURL().then(take).catch(()=>{});
    AsyncStorage.getItem('vigil_pending_code').then(c=>{if(c)setPendingCode(c);}).catch(()=>{});
    const sub=Linking.addEventListener('url',e=>take(e.url));
    return()=>sub.remove();
  },[]);
  const [demoMode,setDemoMode]         = useState(false);

  // ── Connection ──
  const [initialized,setInitialized]   = useState(false);
  const [initError,setInitError]       = useState<string|null>(null);
  const [jetsonOk,setJetsonOk]         = useState<boolean|null>(null);
  const [cloud,setCloud]               = useState<CloudCfg|null>(null);   // CLOUD_V1
  const [serverVersion,setServerVersion]= useState<string|null>(null);
  const [refreshing,setRefreshing]     = useState(false);

  // ── Sync ──
  const [syncing,setSyncing]           = useState(false);
  const [lastSync,setLastSync]         = useState<string|null>(null);
  const [countdown,setCountdown]       = useState(AUTO_SYNC_SEC);
  const [lastIns,setLastIns]           = useState<number|null>(null);
  const [lastSkip,setLastSkip]         = useState<number|null>(null);

  // ── Data ──
  const [today,setToday]               = useState<DailySummary|null>(null);
  // HOME_PEOPLE_V1: the Today screen can show any named person.
  const [homePid,setHomePid]           = useState<string|null>(null);
  const [personToday,setPersonToday]   = useState<any>(null);
  const [watchOwner,setWatchOwnerS]    = useState<string|null>(null);   // HOME_PEOPLE_V2
  const [showTeach,setShowTeach]       = useState(false);                // TEACH_V1
  const teachOffered=useRef(false);
  // WATCH_OWNER_V2: the server is told too, so health signals and alerts name the right person.
  const pushWatchOwner=(pid:string|null)=>{AX.post(`${urlRef.current||FALLBACK_URL}/config/watch-owner`,{person_id:pid},{timeout:6000}).catch(()=>{});};
  useEffect(()=>{AsyncStorage.getItem(SK_WATCH_OWNER).then(v=>{if(v){setWatchOwnerS(v);setTimeout(()=>pushWatchOwner(v),4000);}}).catch(()=>{});},[]);
  const setWatchOwner=(pid:string)=>{setWatchOwnerS(pid);AsyncStorage.setItem(SK_WATCH_OWNER,pid).catch(()=>{});pushWatchOwner(pid);};
  const clearWatchOwner=()=>{setWatchOwnerS(null);AsyncStorage.removeItem(SK_WATCH_OWNER).catch(()=>{});pushWatchOwner(null);};
  const [history,setHistory]           = useState<DailySummary[]>([]);
  const [alerts,setAlerts]             = useState<AlertEntry[]>([]);
  const [cameraStatus,setCameraStatus] = useState<CameraStatus|null>(null);
  const [cameraFrame,setCameraFrame]   = useState<string|null>(null);
  const [rooms,setRooms]               = useState<Room[]>([]);
  const [activeRoomId,setActiveRoomId] = useState<string>('room_1');
  const [persons,setPersons]                 = useState<Person[]>([]);
  const [personAlerts,setPersonAlerts]       = useState<PersonAlert[]>([]);
  const [showPersonModal,setShowPersonModal] = useState(false);
  const [pendingPersonAlert,setPendingPersonAlert] = useState<PersonAlert|null>(null);
  const [namingPersonId,setNamingPersonId]   = useState<string|null>(null);

  // Demo Mode: populate every screen from canned data and stop all polling.
  // No request is issued at all, so there is nothing to time out or fail.
  const enterDemo = useCallback(()=>{
    setDemoMode(true);
    setToday(DEMO_TODAY);
    setHistory(DEMO_HISTORY);
    setAlerts(DEMO_ALERTS);
    setPersons(DEMO_PERSONS);
    setCameraStatus(DEMO_CAMERA);
    setRooms(DEMO_ROOMS);
    setPersonAlerts([]);
    setCameraFrame(null);
  },[]);

  const exitDemo = useCallback(()=>{
    setDemoMode(false);
    setToday(null); setHistory([]); setAlerts([]);
    setPersons([]); setCameraStatus(null); setRooms([]);
  },[]);

  // Fold a duplicate detection into someone already known. Identity
  // fragments when a person is briefly seen, seen at an unusual distance,
  // or partly occluded; the caregiver can resolve it in one tap.
  const mergePerson = useCallback(async(sourceId:string,targetId:string,alertId?:number)=>{
    const u = urlRef.current||FALLBACK_URL;
    try{
      await AX.post(`${u}/persons/${sourceId}/merge`,
        {target_person_id:targetId},{timeout:8000});
      if(alertId!=null){
        try{ await AX.post(`${u}/persons/alerts/${alertId}/resolve`,{},{timeout:6000}); }
        catch(_e){}
      }
      setShowPersonModal(false); setPendingPersonAlert(null); setNamingPersonId(null);
      fetchPersons();
    }catch(e:any){
      log('ERROR',`Merge failed: ${e?.message??String(e)}`);
    }
    // fetchPersons and log are declared further down the component. Naming
    // them here would read them in the temporal dead zone. Both are stable
    // useCallback values, so the body resolves them correctly at call time.
  },[]);   // eslint-disable-line react-hooks/exhaustive-deps
  const [newPersonName,setNewPersonName]     = useState('');
  const nameRef=useRef('');
  const [isResidentFlag,setIsResidentFlag]   = useState(false);
  const [selectedPerson,setSelectedPerson]   = useState<Person|null>(null);
  const [zoneEditor,setZoneEditor]=useState(false);
  // SETUP_CHECK_V1: ask for the bed and couch areas on first use, and again
  // whenever the server notices the camera has moved.
  const [setupStatus,setSetupStatus]=useState<{needs_zones:boolean;camera_moved:boolean}|null>(null);
  const setupAsked=useRef<string>('');
  const zoneEditorRef=useRef(false);
  useEffect(()=>{zoneEditorRef.current=zoneEditor;},[zoneEditor]);
  const fetchSetupStatus=useCallback(async()=>{
    try{
      const r=await AX.get(`${url()}/camera/setup-status`,{timeout:6000});
      if(r.data?.status!=='ok')return;
      const st={needs_zones:!!r.data.needs_zones,camera_moved:!!r.data.camera_moved};
      setSetupStatus(st);
      const key=st.camera_moved?'moved:'+(r.data.moved_at||''):st.needs_zones?'zones':'';
      if(!key||setupAsked.current===key)return;
      setupAsked.current=key;
      if(st.camera_moved){
        Alert.alert('Did the camera move?',
          "VIGIL's view of the room has changed. Please mark the bed and couch again, so lying down there is not mistaken for a fall.",
          [{text:'Mark them now',onPress:()=>setZoneEditor(true)},
           {text:"It didn't move",onPress:()=>{AX.post(`${url()}/camera/moved/dismiss`,{}).then(fetchSetupStatus).catch(()=>{});}},
           {text:'Later',style:'cancel'}]);
      }else{
        Alert.alert('Mark the bed and couch',
          'Draw a box over each bed and couch the camera can see. Then VIGIL knows lying down there is not a fall. It takes one minute.',
          [{text:'Mark them now',onPress:()=>setZoneEditor(true)},{text:'Later',style:'cancel'}]);
      }
    }catch(_e){}
  },[]);
  // IN_VIEW_LIVE_V1: exactly who is in the camera picture right now.
  const [inViewList,setInViewList]=useState<{id:string;name:string|null;is_resident:number;track?:number|null}[]>([]);
  const [boxAgain,setBoxAgain]=useState(false);   // APP_SETUP_V1: new Wi-Fi or new camera account
  const [whoToday,setWhoToday]=useState<{track:number|null;current:{id:string;name:string|null}|null}|null>(null);   // WHO_SHEET_V1
  const [identifying,setIdentifying]=useState(0);
  // IDENTITY_V6_APP
  const [inViewOthers,setInViewOthers]=useState<{label:string;track:number|null}[]>([]);
  // STATUS_SYNC_V1: the status line agrees with the 'In view now' list.
  const someoneInView = !!cameraStatus?.person_present || inViewList.length>0 || identifying>0;
  const inViewNames = inViewList.map(v=>v.name).filter(Boolean).join(', ');
  const [idMode,setIdMode]=useState<'resident'|'family'|null>(null);
  const loadIdMode=useCallback(async()=>{
    try{ const r=await AX.get(`${urlRef.current||FALLBACK_URL}/identity/mode`,{timeout:5000}); setIdMode(r.data?.mode==='resident'?'resident':'family'); }catch(_e){}
  },[]);
  const saveIdMode=useCallback(async(mode:'resident'|'family',residentId?:string)=>{
    try{
      await AX.post(`${urlRef.current||FALLBACK_URL}/identity/mode`,{mode,resident_person_id:residentId},{timeout:6000});
      setIdMode(mode);
    }catch(_e){ Alert.alert('Could not change the setting','Check the connection to the camera unit.'); }
  },[]);
  const [fullscreen,setFullscreen]     = useState(false);
  const [expandedAlert,setExpandedAlert]= useState<number|null>(null);
  const [replayFrames,setReplayFrames]  = useState<ReplayFrame[]|null>(null);
  const [replayMsg,setReplayMsg]        = useState<string|null>(null);

  // ── Settings ──
  const [residentName,setResidentName]   = useState('');
  const [residentHeight,setResidentHeight]= useState('170');
  // SETTINGS_V2: everything asked at sign-up can be changed later in Settings > Person.
  const [pUnits,setPUnits]   = useState<'us'|'metric'>('us');
  const [hFt,setHFt]         = useState(''); const [hIn,setHIn]=useState(''); const [hCm,setHCm]=useState('');
  const [wTxt,setWTxt]       = useState(''); const [pAge,setPAge]=useState('');
  const fillEditors=(cm:number|null,kg:number|null,u:'us'|'metric')=>{
    if(cm&&cm>0){const ti=cm/2.54;let ft=Math.floor(ti/12),inch=Math.round(ti-ft*12);if(inch===12){ft+=1;inch=0;}
      setHFt(String(ft));setHIn(String(inch));setHCm(String(Math.round(cm)));}
    if(kg!=null&&kg>0)setWTxt(u==='us'?String(Math.round(kg/0.45359237)):String(Math.round(kg*10)/10));
  };
  const editorCm=():number|null=>{
    if(pUnits==='metric'){const v=parseFloat(hCm);return v>=90&&v<=230?v:null;}
    const ft=parseFloat(hFt)||0,inch=parseFloat(hIn)||0;const v=(ft*12+inch)*2.54;return v>=90&&v<=230?v:null;
  };
  const editorKg=():number|null=>{
    const v=parseFloat(wTxt);if(!(v>0))return null;
    const kg=pUnits==='us'?v*0.45359237:v;return kg>=10&&kg<=300?Math.round(kg*10)/10:null;
  };
  const switchUnits=(u:'us'|'metric')=>{if(u===pUnits)return;const cm=editorCm(),kg=editorKg();setPUnits(u);fillEditors(cm,kg,u);};
  const [emergencyPhone,setEmergencyPhone]= useState('');
  const [responderName,setResponderName]  = useState('');
  const [trendView,setTrendView]          = useState<'week'|'baseline'>('week');
  const [trendPid,setTrendPid]            = useState<string|null>(null);   // TRENDS_INLINE_V1
  useEffect(()=>{AsyncStorage.getItem('vigil_caregiver_name')
    .then(v=>{if(v)setResponderName(v);}).catch(()=>{});},[]);
  const [emergencyEmail,setEmergencyEmail]= useState('');
  const [watchEnabled,setWatchEnabled]   = useState(false);
  const [serverIp,setServerIp]           = useState(DEFAULT_IP);
  const [serverPort,setServerPort]       = useState(DEFAULT_PORT);
  const [advancedOpen,setAdvancedOpen]   = useState(false);
  const [savingProfile,setSavingProfile] = useState(false);

  // ── UI ──
  const [activeTab,setActiveTab]         = useState<MainTab>('today');
  const [settingsTab,setSettingsTab]     = useState<SettingTab>('resident');
  const [showLogs,setShowLogs]           = useState(false);
  const [debugLog,setDebugLog]           = useState<DebugEntry[]>([]);

  // ── Refs ──
  const syncInFlight  = useRef(false);
  const autoTimer     = useRef<ReturnType<typeof setInterval>|null>(null);
  const cntTimer      = useRef<ReturnType<typeof setInterval>|null>(null);
  const urlRef        = useRef(FALLBACK_URL);
  const prevFrame     = useRef<string|null>(null);
  // PRIVACY_VIEW_V1: show residents as stick figures by default; video only on request.
  const [privacyView,setPrivacyView] = useState(true);
  const privacyViewRef = useRef(true);
  useEffect(()=>{AsyncStorage.getItem('vigil_privacy_view').then(v=>{if(v==='0'){setPrivacyView(false);privacyViewRef.current=false;}}).catch(()=>{});},[]);
  const togglePrivacyView=useCallback(()=>{
    const next=!privacyViewRef.current; privacyViewRef.current=next; setPrivacyView(next);
    prevFrame.current=null; setCameraFrame(null);
    AsyncStorage.setItem('vigil_privacy_view',next?'1':'0').catch(()=>{});
  },[]);
  const appStateRef   = useRef<AppStateStatus>('active');

  const alertCount = alerts.filter(a=>!a.seen).length;
  const url = ()=>urlRef.current||FALLBACK_URL;

  // ── Logging ──
  const log = useCallback((level:DebugEntry['level'],msg:string)=>{
    if (__DEV__) console.log(`[VIGIL][${level}] ${msg}`);
    setDebugLog(p=>[{ts:new Date().toLocaleTimeString(),level,msg},...p].slice(0,150));
  },[]);

  // ─────────────────────────────────────────────
  // BOOT — check onboarding and load saved settings
  // ─────────────────────────────────────────────
  useEffect(()=>{
    (async()=>{
      try{
        const [done,profileStr,serverStr,cloudStr]=await Promise.all([
          AsyncStorage.getItem(SK_ONBOARDING),
          AsyncStorage.getItem(SK_PROFILE),
          AsyncStorage.getItem(SK_SERVER),
          AsyncStorage.getItem(SK_CLOUD),
        ]);
        if(profileStr){
          const p:SavedProfile=JSON.parse(profileStr);
          setResidentName(p.residentName||'');
          setResidentHeight(p.residentHeight||'170');
          setEmergencyPhone(p.emergencyPhone||'');
          setEmergencyEmail(p.emergencyEmail||'');
          setWatchEnabled(p.watchEnabled||false);
          const u=(p.units==='metric'?'metric':'us') as 'us'|'metric';setPUnits(u);setPAge(p.age||'');
          fillEditors(parseFloat(p.residentHeight)||null,parseFloat(p.weightKg||'')||null,u);
        }
        if(serverStr){
          const sv=JSON.parse(serverStr);
          const ip=sv.ip||DEFAULT_IP; const port=sv.port||DEFAULT_PORT;
          setServerIp(ip);setServerPort(port);
          const u=`http://${ip}:${port}`;urlRef.current=u;
        }
        if(cloudStr){
          try{
            const cfg:CloudCfg=JSON.parse(cloudStr);
            if(cfg?.token&&cfg?.siteId){applyCloudHeaders(cfg);setCloud(cfg);urlRef.current=cloudSiteUrl(cfg);}
          }catch(_e){}
        }
        if(done!=='done'&&!cloudStr){setOnboarding(true);}
      }catch{setOnboarding(true);}
      finally{setBooting(false);}
    })();
  },[]);

  // ─────────────────────────────────────────────
  // NETWORK
  // ─────────────────────────────────────────────
  const pingJetson=useCallback(async()=>{
    const u=url();
    try{
      const r=await AX.get(`${u}/health`);
      if(r.data?.status==='ok'){
        setJetsonOk(true);setServerVersion(r.data?.version??null);
      }else{setJetsonOk(false);}
    }catch{setJetsonOk(false);}
  },[]);

  const fetchDashboard=useCallback(async()=>{
    try{
      const [t,h]=await Promise.all([AX.get(`${url()}/summary/today`),AX.get(`${url()}/summary/recent`)]);
      if(t.data?.date)setToday(t.data);
      if(Array.isArray(h.data))setHistory(h.data);
    }catch(e:any){log('ERROR',`Dashboard: ${e?.message}`);}
  },[log]);

  const fetchAlerts=useCallback(async()=>{
    try{
      const r=await AX.get(`${url()}/alerts/all`);
      if(Array.isArray(r.data))setAlerts(r.data);
    }catch(_){}
  },[]);

  const fetchCameraStatus=useCallback(async()=>{
    try{
      const r=await AX.get(`${url()}/camera/status`,{timeout:5000});
      if(r.data)setCameraStatus(r.data);
    }catch{setCameraStatus({online:false,fps:null,person_present:false,last_seen_minutes_ago:null,camera_mode:null});}
  },[]);

  const fetchCameraSnapshot=useCallback(async()=>{
    try{
      const r=await AX.get(`${url()}/camera/snapshot${privacyViewRef.current?'?view=skeleton&w=800':'?w=800'}`,{timeout:4000});
      if(r.data?.available&&r.data?.frame){
        // Only update if frame actually changed to reduce flicker
        if(r.data.frame!==prevFrame.current){
          prevFrame.current=r.data.frame;
          setCameraFrame(r.data.frame);
        }
      }
    }catch(_){}
  },[]);

  const fetchRooms=useCallback(async()=>{
    try{
      const r=await AX.get(`${url()}/rooms`,{timeout:5000});
      if(Array.isArray(r.data))setRooms(r.data);
    }catch(_){}
  },[]);

  const fetchPersons=useCallback(async()=>{
    try{
      const r=await AX.get(`${url()}/persons`,{timeout:5000});
      if(Array.isArray(r.data))setPersons(r.data);
    }catch(_){}
  },[]);

  // Refresh data when app comes back to foreground after being backgrounded.
  // Placed after pingJetson/fetchDashboard/fetchAlerts/fetchCameraStatus/fetchPersons
  // so all five dependencies are already declared (TS2448/TS2454 fix).
  useEffect(()=>{
    const sub = AppState.addEventListener('change', (next: AppStateStatus)=>{
      const wasBackground = appStateRef.current !== 'active';
      appStateRef.current = next;
      if(next === 'active' && wasBackground && jetsonOk === true){
        pingJetson();
        fetchDashboard();
        fetchAlerts();
        fetchPersons();
        fetchCameraStatus();
      }
    });
    return ()=> sub.remove();
  },[jetsonOk, pingJetson, fetchDashboard, fetchAlerts, fetchPersons, fetchCameraStatus]);

  // "Not now" hides a prompt for 30 minutes; without this the 6 s poll
  // reopened the sheet immediately after it was dismissed.
  const snoozedRef = useRef<{[id:number]:number}>({});
  const fetchPersonAlerts=useCallback(async()=>{
    try{
      const r=await AX.get(`${url()}/persons/alerts`,{timeout:5000});
      const now=Date.now();
      const list:PersonAlert[]=Array.isArray(r.data)
        ?r.data.filter((a:PersonAlert)=>!((snoozedRef.current[a.id]??0)>now)):[];
      setPersonAlerts(list);
      if(list.length>0){
        setPendingPersonAlert(prev=>{
          if(!showPersonModal && !prev){
            setShowPersonModal(true);
            return list[list.length-1];   // server sends newest first; ask about the oldest
          }
          return prev;
        });
      }
    }catch(_){}
  },[showPersonModal]);

  // PEOPLE_UI_V1: edit someone who is already named.
  const savePersonEdit=useCallback(async(p:Person,name:string,isRes:boolean)=>{
    try{
      await AX.post(`${url()}/persons/${p.id}/identify`,{name,is_resident:isRes},{timeout:8000});
    }catch(e:any){log('ERROR',`Edit person: ${e?.message}`);}
    fetchPersons();
  },[fetchPersons,log]);
  // DELETE_PERSON_APP
  const deletePerson=useCallback((p:Person)=>{
    Alert.alert(`Delete ${p.name??'this person'}?`,
      'Their walking history, baseline and appearance memory are removed. Past alerts stay. This cannot be undone.',[
      {text:'Cancel',style:'cancel'},
      {text:'Delete',style:'destructive',onPress:async()=>{
        try{ await AX.post(`${url()}/persons/${p.id}/delete`,{},{timeout:8000}); }
        catch(_e){ Alert.alert('Could not delete','Check the connection to the camera unit.'); }
        fetchPersons();
      }},
    ]);
  },[fetchPersons]);
  // PHOTO_PICK_V1: the caregiver chooses a clear photo, or uses initials.
  const choosePhoto=useCallback(async(p:Person)=>{
    try{
      const r:any=await launchImageLibrary({mediaType:'photo',includeBase64:true,maxWidth:360,maxHeight:360,quality:0.8,selectionLimit:1});
      if(r?.didCancel) return;
      const b64=r?.assets?.[0]?.base64;
      if(!b64){ Alert.alert('Could not read that photo','Try a different picture.'); return; }
      await AX.post(`${url()}/persons/${p.id}/photo/set`,{photo_b64:b64},{timeout:15000});
      bumpPhoto(p.id);
    }catch(_e){ Alert.alert('Could not update the photo','Check the connection to the camera unit.'); }
  },[]);
  const setInitials=useCallback(async(p:Person)=>{
    try{ await AX.post(`${url()}/persons/${p.id}/photo/clear`,{},{timeout:8000}); bumpPhoto(p.id); }
    catch(_e){ Alert.alert('Could not update the photo','Check the connection to the camera unit.'); }
  },[]);
  const editPerson=useCallback((p:Person)=>{
    const others=persons.filter(o=>o.id!==p.id&&!!o.name);
    Alert.alert(p.name??'Person','What would you like to change?',[
      {text:'Rename',onPress:()=>Alert.prompt('Rename',`New name for ${p.name}`,[
        {text:'Cancel',style:'cancel'},
        {text:'Save',onPress:(v?:string)=>{const n=(v||'').trim();if(n)savePersonEdit(p,n,!!p.is_resident);}},
      ],'plain-text',p.name??'')},
      {text:p.height_cm?`Height (${fmtHeight(p.height_cm,pUnits)})`:'Set height',onPress:()=>Alert.prompt(`${p.name}'s height`,
        pUnits==='us'?'For example 5\'6" or 66 in. Sets their walking speed.':'In centimetres, for example 168. Sets their walking speed.',[
        {text:'Cancel',style:'cancel'},
        {text:'Save',onPress:async(v?:string)=>{const h=parseHeightCm(v||'');
          if(h==null){Alert.alert('Check the height',pUnits==='us'?'Use feet and inches, like 5\'6".':'Use centimetres, like 168.');return;}
          try{await AX.post(`${url()}/persons/${p.id}/height`,{height_cm:h},{timeout:8000});fetchPersons();}
          catch(_e){Alert.alert('Could not save','Check the connection to the camera.');}}},
      ],'plain-text',p.height_cm?fmtHeight(p.height_cm,pUnits):'')},
      {text:'Choose photo',onPress:()=>choosePhoto(p)},
      {text:'Use initials instead of photo',onPress:()=>setInitials(p)},
      {text:p.is_resident?'Mark as visitor':'Mark as resident',onPress:()=>savePersonEdit(p,p.name??'Person',!p.is_resident)},
      ...(others.length>0?[{text:'Same person as another entry',onPress:()=>Alert.alert(
        `Merge ${p.name}`,'Everything recorded for this entry moves to the person you choose, and this entry is removed.',
        [...others.slice(0,6).map(o=>({text:`Merge into ${o.name}`,style:'destructive' as const,onPress:()=>mergePerson(p.id,o.id)})),
         {text:'Cancel',style:'cancel' as const}])}]:[]),
      {text:'Delete',style:'destructive',onPress:()=>deletePerson(p)},
      {text:'Cancel',style:'cancel'},
    ]);
  },[persons,savePersonEdit,mergePerson,deletePerson,choosePhoto,setInitials,pUnits,fetchPersons]);

  const identifyPerson=useCallback(async(personId:string,name:string,isRes:boolean,alertId?:number)=>{
    try{
      await AX.post(`${url()}/persons/${personId}/identify`,{name,is_resident:isRes});
      if(alertId){await AX.post(`${url()}/persons/alerts/${alertId}/resolve`,{});}
    }catch(e:any){log('ERROR',`Identify: ${e?.message}`);}
    setShowPersonModal(false);
    setPendingPersonAlert(null);
    setNamingPersonId(null);
    setNewPersonName('');
    await fetchPersons();
    await fetchPersonAlerts();
  },[fetchPersons,fetchPersonAlerts,log]);

  const acknowledgeAlert=useCallback(async(id:number)=>{
    // Only mark it seen if the server confirms. The previous version
    // swallowed every error and marked it acknowledged regardless, so the
    // button appeared to work for two months while nothing was recorded.
    if(demoMode){
      // Demo Mode has no server. Update locally so the button works for a
      // reviewer instead of silently failing.
      setAlerts(p=>p.map(a=>a.id===id?{...a,seen:1,acknowledged_by:'Demo caregiver'} as any:a));
      return;
    }
    try{
      const who = await caregiverName();
      await AX.post(`${url()}/alerts/${id}/ack`,{acknowledged_by:who});
      setAlerts(p=>p.map(a=>a.id===id?{...a,seen:1,acknowledged_by:who} as any:a));
    }catch(e:any){
      log('ERROR',`Could not acknowledge alert ${id}: ${e?.message??String(e)}`);
    }
  },[log,demoMode]);

  const loadConfig=useCallback(async()=>{
    try{
      const r=await AX.get(`${url()}/config`);
      if(r.data){
        if(r.data.resident_name)setResidentName(r.data.resident_name);
        if(r.data.emergency_phone)setEmergencyPhone(r.data.emergency_phone);
        if(r.data.emergency_email)setEmergencyEmail(r.data.emergency_email);
        if(r.data.resident_height_cm)setResidentHeight(String(r.data.resident_height_cm));
        const u=(r.data.units==='metric'?'metric':(r.data.units==='us'?'us':null)) as 'us'|'metric'|null;
        if(u)setPUnits(u);
        if(r.data.resident_age)setPAge(String(r.data.resident_age));
        if(r.data.has_apple_watch!=null)setWatchEnabled(!!r.data.has_apple_watch);
        fillEditors(r.data.resident_height_cm||null,r.data.resident_weight_kg??null,u||'us');
      }
    }catch(_){}
  },[]);

  // ─────────────────────────────────────────────
  // HEALTHKIT INIT
  // ─────────────────────────────────────────────
  // PEOPLE_UI_V1: HealthKit start is a function so the status pill can retry it.
  const startHealthKit=useCallback(()=>{
    setInitError(null);
    if(!AppleHealthKit){
      const m=`HealthKit is not available in this build (${HK_LOAD_ERROR??'unknown load failure'}).`;
      log('WARN',m);setInitError(m);return;
    }
    const HKP=AppleHealthKit?.Constants?.Permissions;
    if(!HKP||typeof AppleHealthKit.initHealthKit!=='function'){
      const m='The HealthKit native module is not linked. Run pod install in the ios folder, then Clean Build Folder in Xcode.';
      log('ERROR',m);setInitError(m);return;
    }
    const perms={permissions:{read:[
      HKP.StepCount,
      HKP.RestingHeartRate,
      HKP.HeartRateVariability,
      HKP.HeartRate,
      HKP.SleepAnalysis,
      HKP.ActiveEnergyBurned,
      HKP.DistanceWalkingRunning,
      HKP.OxygenSaturation,
      HKP.RespiratoryRate,
    ],write:[] as string[]}};
    log('INFO','Initializing HealthKit...');
    AppleHealthKit.initHealthKit(perms,(err:any)=>{
      if(err){
        const m=typeof err==='string'?err:(err?.message??JSON.stringify(err));
        log('ERROR',`HK: ${m}`);setInitError(m);return;
      }
      log('OK','HealthKit ready');setInitError(null);setInitialized(true);
    });
  },[log]);
  useEffect(()=>{startHealthKit();},[startHealthKit]);

  // ─────────────────────────────────────────────
  // SERVER POLLING EFFECTS
  // ─────────────────────────────────────────────
  useEffect(()=>{if(!booting&&!showOnboarding&&!demoMode)setTimeout(()=>pingJetson(),1500);},[booting,showOnboarding,demoMode,pingJetson]);

  useEffect(()=>{
    if(!initialized||jetsonOk!==true||demoMode)return;
    fetchDashboard();fetchAlerts();loadConfig();fetchRooms();
    setCountdown(AUTO_SYNC_SEC);
    // The per-second countdown re-rendered the whole app every second; removed.
    autoTimer.current=setInterval(()=>{if(!kbOpenRef.current)performSync(true);},AUTO_SYNC_SEC*1000);
    performSync(true);
    return()=>{if(autoTimer.current)clearInterval(autoTimer.current);if(cntTimer.current)clearInterval(cntTimer.current);};
  },[initialized,jetsonOk]); // eslint-disable-line

  useEffect(()=>{if(!jetsonOk||demoMode||showPersonModal)return;fetchCameraStatus();const id=setInterval(()=>{if(!kbOpenRef.current)fetchCameraStatus();},10000);return()=>clearInterval(id);},[jetsonOk,showPersonModal,fetchCameraStatus]);
  useEffect(()=>{if(!jetsonOk||demoMode||showPersonModal||activeTab!=='today')return;let alive=true;(async()=>{while(alive){try{if(!kbOpenRef.current)await fetchCameraSnapshot();}catch(_e){}await new Promise(r=>setTimeout(r,CLOUD_ACTIVE?400:100));}})();return()=>{alive=false;};},[jetsonOk,showPersonModal,activeTab,fetchCameraSnapshot]);
  // Camera-derived metrics (gait speed / cadence / posture) refresh every 4s so they
  // visibly move while someone is walking in frame — the 60s HealthKit auto-sync
  // (above) is far too slow for this to feel live during a walk-test or demo.
  useEffect(()=>{if(!jetsonOk||demoMode||showPersonModal)return;fetchDashboard();const id=setInterval(()=>{if(!kbOpenRef.current)fetchDashboard();},4000);return()=>clearInterval(id);},[jetsonOk,showPersonModal,fetchDashboard]);
  useEffect(()=>{if(!jetsonOk||demoMode||showPersonModal)return;const id=setInterval(()=>{if(!kbOpenRef.current)fetchRooms();},15000);return()=>clearInterval(id);},[jetsonOk,showPersonModal,fetchRooms]);
  // HOME_PEOPLE_V2: every person, the resident included, shows only their own
  // numbers from /persons/<id>/today. The answer is tagged with whose it is, so a
  // late reply for the previous person can never be shown under the new one.
  const _named=persons.filter(p=>!!p.name);
  const selPid:string|null=(homePid&&_named.some(p=>p.id===homePid))?homePid
    :(_named.find(p=>!!p.is_resident)?.id??_named[0]?.id??null);
  useEffect(()=>{
    if(!selPid||!jetsonOk||demoMode){setPersonToday(null);return;}
    let alive=true;
    const pid=selPid;
    const load=()=>AX.get(`${url()}/persons/${encodeURIComponent(pid)}/today`,{timeout:8000})
      .then(r=>{if(alive)setPersonToday({...r.data,_pid:pid});}).catch(()=>{});
    setPersonToday(null);load();
    const id=setInterval(()=>{if(!kbOpenRef.current)load();},10000);
    return()=>{alive=false;clearInterval(id);};
  },[selPid,jetsonOk,demoMode]);
  useEffect(()=>{if(!jetsonOk||demoMode)return;fetchSetupStatus();const id=setInterval(()=>{if(!kbOpenRef.current&&!zoneEditorRef.current)fetchSetupStatus();},30000);return()=>clearInterval(id);},[jetsonOk,demoMode,fetchSetupStatus]);
  useEffect(()=>{if(!jetsonOk||demoMode||showPersonModal)return;fetchPersons();const id=setInterval(()=>{if(!kbOpenRef.current)fetchPersons();},8000);return()=>clearInterval(id);},[jetsonOk,showPersonModal,fetchPersons]);
  // Tightened from 30s -> 6s: this is the "more than one person detected" identification
  // sheet — for a live demo this needs to feel near-instant when a second person walks in.
  useEffect(()=>{if(!jetsonOk||demoMode||showPersonModal)return;fetchPersonAlerts();const id=setInterval(()=>{if(!kbOpenRef.current)fetchPersonAlerts();},6000);return()=>clearInterval(id);},[jetsonOk,showPersonModal,fetchPersonAlerts]);
  // Tightened from 15s -> 5s: fall/stillness alerts should reach the UI almost as fast
  // as the SMS itself arrives, not lag up to 15s behind it.
  useEffect(()=>{if(!jetsonOk||demoMode||showPersonModal)return;const id=setInterval(()=>{if(!kbOpenRef.current)fetchAlerts();},5000);return()=>clearInterval(id);},[jetsonOk,showPersonModal,fetchAlerts]);

  // ─────────────────────────────────────────────
  useEffect(()=>{
    if(!jetsonOk||demoMode||activeTab!=='today')return;
    let alive=true;
    const tick=async()=>{
      if(kbOpenRef.current)return;
      try{
        const r=await AX.get(`${urlRef.current||FALLBACK_URL}/persons/in-view`,{timeout:3000});
        if(alive){ setInViewList(Array.isArray(r.data?.people)?r.data.people:[]); setIdentifying(Number(r.data?.identifying??0)); setInViewOthers(Array.isArray(r.data?.others)?r.data.others:[]); }
      }catch(_e){}
    };
    tick(); const id=setInterval(tick,2000);
    return()=>{alive=false;clearInterval(id);};
  },[jetsonOk,demoMode,activeTab]);
  // HEALTHKIT SYNC
  // ─────────────────────────────────────────────
  const performSync=useCallback(async(silent=false)=>{
    if(syncInFlight.current||!initialized||jetsonOk!==true||demoMode)return;
    syncInFlight.current=true;setSyncing(true);
    const tr=todayRange();const rr=rollingRange(168);
    try{
      const[steps,cals,dist]=await Promise.all([
        readDailyTotal('getStepCount',tr,'StepCount','count',log),
        readDailyTotal('getActiveEnergyBurned',tr,'ActiveEnergyBurned','kcal',log),
        readDailyTotal('getDistanceWalkingRunning',tr,'DistanceWalkingRunning','m',log),
      ]);
      const[rhr,hrv,spo2,resp,sleep]=await Promise.all([
        readSamples('getRestingHeartRate',tr,'RestingHeartRate','bpm',log),
        readSamples('getHeartRateVariabilitySamples',tr,'HeartRateVariabilitySDNN','ms',log),
        readSamples('getOxygenSaturationSamples',rr,'OxygenSaturation','ratio',log),
        readSamples('getRespiratoryRateSamples',rr,'RespiratoryRate','brpm',log),
        readSamples('getSleepSamples',rr,'SleepAnalysis','hr',log),
      ]);
      const spo2Pct=spo2.map(s=>({...s,value:s.value<=1?s.value*100:s.value,unit:'%'}));
      const samples=[...steps,...cals,...dist,...rhr,...hrv,...spo2Pct,...resp,...sleep];
      if(!samples.length){if(!silent)log('WARN','No samples');return;}
      const r=await AX.post(`${url()}/sync/healthkit`,{samples});
      setLastIns(r.data?.inserted??0);setLastSkip(r.data?.skipped??0);
      setLastSync(new Date().toLocaleTimeString());
      log('OK',`Sync: +${r.data?.inserted??0}`);
      await fetchDashboard();
    }catch(e:any){log('ERROR',`Sync: ${e?.message}`);if(!silent)Alert.alert('Sync Failed',e?.message);}
    finally{setSyncing(false);syncInFlight.current=false;}
  },[initialized,jetsonOk,log,fetchDashboard]);

  // ─────────────────────────────────────────────
  // SAVE PROFILE
  // ─────────────────────────────────────────────
  const saveProfile=async()=>{
    const cm=editorCm(),kg=null as number|null;
    if(!residentName.trim()){Alert.alert('Name needed','Type the name of the person being watched.');return;}
    if(cm==null){Alert.alert('Check the height',pUnits==='us'?'Use feet and inches, for example 5 ft 6 in.':'Use centimetres, for example 168.');return;}
    setSavingProfile(true);
    try{
      await AX.post(`${url()}/config`,{
        resident_name:residentName.trim(),
        emergency_phone:emergencyPhone||null,
        emergency_email:emergencyEmail||null,
        resident_height_cm:Math.round(cm),
        units:pUnits, has_apple_watch:watchEnabled?1:0,
      });
      setResidentHeight(String(Math.round(cm)));
      const profile:SavedProfile={residentName:residentName.trim(),residentHeight:String(Math.round(cm)),emergencyPhone,emergencyEmail,watchEnabled,
        weightKg:kg!=null?String(kg):'',age:pAge.trim(),units:pUnits};
      await AsyncStorage.setItem(SK_PROFILE,JSON.stringify(profile));
      Alert.alert('Saved','Changes saved.');
    }catch(e:any){Alert.alert('Save failed',`Could not reach VIGIL: ${e?.message}`);}
    finally{setSavingProfile(false);}
  };

  // TEACH_V1: offer "Introduce everyone" once, the first time a camera is reachable.
  useEffect(()=>{
    if(!jetsonOk||demoMode||teachOffered.current)return;
    teachOffered.current=true;
    const key='vigil_teach_offered_'+(cloud?.siteId||'local');
    AsyncStorage.getItem(key).then(v=>{if(!v){AsyncStorage.setItem(key,'1').catch(()=>{});setTimeout(()=>setShowTeach(true),1500);}}).catch(()=>{});
  },[jetsonOk,demoMode,cloud?.siteId]);

  // CLOUD_V1: switch the whole app to another camera, or in and out of the cloud.
  const resetSiteData=()=>{
    Object.keys(PHOTO_CACHE).forEach(k=>delete PHOTO_CACHE[k]);
    prevFrame.current=null;setCameraFrame(null);
    setPersons([]);setAlerts([]);setRooms([]);
    setJetsonOk(null);
  };
  const enterCloud=async(cfg:CloudCfg|null)=>{
    applyCloudHeaders(cfg);setCloud(cfg);resetSiteData();
    if(cfg){
      urlRef.current=cloudSiteUrl(cfg);
      await AsyncStorage.setItem(SK_CLOUD,JSON.stringify(cfg));
      await AsyncStorage.setItem(SK_ONBOARDING,'done');
    }else{
      await AsyncStorage.removeItem(SK_CLOUD);
      urlRef.current=`http://${serverIp}:${serverPort}`;
    }
    setTimeout(()=>pingJetson(),300);
  };
  const switchSite=(siteId:string)=>{
    if(!cloud||siteId===cloud.siteId)return;
    enterCloud({...cloud,siteId});
  };
  const refreshCloudSites=useCallback(async()=>{
    if(!cloud)return;
    try{
      const r=await AX.get(`${cloud.gateway}/auth/me`,{timeout:8000});
      const sites:CloudSite[]=r.data?.sites||[];
      if(sites.length){
        const siteId=sites.some(x=>x.id===cloud.siteId)?cloud.siteId:sites[0].id;
        const next={...cloud,sites,siteId};setCloud(next);
        if(siteId!==cloud.siteId){urlRef.current=cloudSiteUrl(next);resetSiteData();}
        AsyncStorage.setItem(SK_CLOUD,JSON.stringify(next)).catch(()=>{});
      }
    }catch(e:any){
      if(e?.response?.status===401){
        Alert.alert('Signed out','Your VIGIL Cloud session ended. Please sign in again.');
        enterCloud(null);
      }
    }
  },[cloud]);
  useEffect(()=>{if(!cloud)return;refreshCloudSites();const id=setInterval(refreshCloudSites,60000);return()=>clearInterval(id);},[cloud?.token]);

  const reconnect=()=>{
    const u=`http://${serverIp}:${serverPort}`;
    urlRef.current=u;setJetsonOk(null);
    AsyncStorage.setItem(SK_SERVER,JSON.stringify({ip:serverIp,port:serverPort}));
    setTimeout(()=>pingJetson(),300);
  };

  const onRefresh=async()=>{setRefreshing(true);await pingJetson();await fetchDashboard();await fetchAlerts();setRefreshing(false);};

  // ─────────────────────────────────────────────
  // CAMERA FEED COMPONENT
  // ─────────────────────────────────────────────
  const camBorder=cameraStatus?.online?C.green+'55':C.border;

  // Drawn in place, not as a component: an inner component is rebuilt on
  // every render, which reloaded the picture and made the feed stutter.
  const renderCameraFeed=(fullSize:boolean=false)=>{
    const w=fullSize?SW-32:CAM_W;
    const h=fullSize?Math.round((SW-32)*9/16):CAM_H;
    return(
      <View style={jetsonOk===true&&cameraFrame?{borderRadius:20,shadowColor:C.green,shadowOpacity:0.15,shadowRadius:16,shadowOffset:{width:0,height:0}}:undefined}>
      <View style={{width:w,height:h,borderRadius:20,overflow:'hidden',borderWidth:1.5,borderColor:camBorder,backgroundColor:C.card}}>
        {cameraFrame?(
          <>
            <Image
              source={{uri:`data:image/jpeg;base64,${cameraFrame}`}}
              style={{width:w,height:h}} resizeMode="cover"
              fadeDuration={0}/>
            <TouchableOpacity
              style={{position:'absolute',top:10,right:10,backgroundColor:'rgba(0,0,0,0.65)',borderRadius:6,flexDirection:'row',alignItems:'center',paddingHorizontal:8,paddingVertical:4,gap:5}}
              onPress={()=>setFullscreen(true)}>
              <Animated.View style={{width:6,height:6,borderRadius:3,backgroundColor:C.green,transform:[{scale:LIVE_PULSE}]}}/>
              <Text style={{color:'#fff',fontSize:11,fontWeight:'700',letterSpacing:1}}>LIVE</Text>
            </TouchableOpacity>
            <TouchableOpacity
              style={{position:'absolute',bottom:10,right:10,backgroundColor:'rgba(0,0,0,0.65)',borderRadius:8,paddingHorizontal:10,paddingVertical:5}}
              onPress={()=>setFullscreen(true)}>
              <Text style={{color:'#fff',fontSize:11,fontWeight:'600'}}>Full Screen</Text>
            </TouchableOpacity>
            <TouchableOpacity
              style={{position:'absolute',top:10,left:10,backgroundColor:'rgba(0,0,0,0.65)',borderRadius:8,paddingHorizontal:10,paddingVertical:5}}
              onPress={togglePrivacyView}>
              <Text style={{color:'#fff',fontSize:11,fontWeight:'600'}}>{privacyView?'Stick figures (tap for video)':'Video (tap for stick figures)'}</Text>
            </TouchableOpacity>
          </>
        ):(
          <View style={{flex:1,alignItems:'center',justifyContent:'center',gap:6}}>
            <Text style={{color:C.muted,fontSize:14}}>
              {demoMode?'Live View \u2014 Demo':'Live View Unavailable'}
            </Text>
            <Text style={{color:C.muted,fontSize:11,textAlign:'center',paddingHorizontal:28,lineHeight:16}}>
              {demoMode
                ?'Live video needs the VIGIL camera unit. Everything else on this screen is sample data.'
                :(cameraStatus?.online?'Camera starting...':'Camera offline')}
            </Text>
          </View>
        )}
      </View>
      </View>
    );
  };

  // ─────────────────────────────────────────────
  // TODAY SCREEN
  // ─────────────────────────────────────────────
  const namedPeople=persons.filter(p=>!!p.name);
  const namedPeopleAll=namedPeople;
  const residentPerson=namedPeople.find(p=>!!p.is_resident)??null;
  const trendPerson=trendPid?(namedPeople.find(p=>p.id===trendPid)??null):null;
  const homePerson=selPid?(namedPeople.find(p=>p.id===selPid)??null):null;
  const dayName=homePerson?homePerson.name:residentName;
  const dayData:any=(selPid&&!demoMode)?(personToday?._pid===selPid?personToday:null):today;
  const dayLoading=!!selPid&&!dayData&&!demoMode;
  // The Apple Watch belongs to whoever this phone is synced for; only their chip shows it.
  const watchOwnerOk=!!watchOwner&&namedPeople.some(p=>p.id===watchOwner);
  // WATCH_OWNER_V2: with nobody picked the page is about the resident, so only show the watch if it is theirs.
  const watchHere=demoMode||(watchOwnerOk?(selPid?selPid===watchOwner:watchOwner===residentPerson?.id):(!selPid&&namedPeople.length===0));
  const hasWatchData=today?.step_count!=null||today?.resting_hr!=null||today?.hrv_sdnn!=null||today?.sleep_hours!=null;
  const renderToday=()=>(
    <ScrollView refreshControl={<RefreshControl refreshing={refreshing} onRefresh={onRefresh} tintColor={C.green}/>} showsVerticalScrollIndicator={false}>
      <View style={s.pageHead}>
        <View style={{flexDirection:'row',alignItems:'center',gap:10,marginBottom:16}}>
          <Image source={{uri:'LaunchLogo'}} style={{width:30,height:30,borderRadius:8}}/>
          <Text style={{color:C.text,fontSize:15,fontWeight:'800',letterSpacing:2.4}}>VIGIL</Text>
        </View>
        <Text style={s.pageTitle}>Today</Text>
        <Text style={s.pageSub}>{new Date().toLocaleDateString(undefined,{weekday:'long',month:'long',day:'numeric'})}</Text>
        {cloud&&cloud.sites.length>1&&(
          <ScrollView horizontal showsHorizontalScrollIndicator={false} style={{marginTop:12}} contentContainerStyle={{gap:8}}>
            {cloud.sites.map(site=>{const on=site.id===cloud.siteId;return(
              <TouchableOpacity key={site.id} onPress={()=>switchSite(site.id)}
                style={{flexDirection:'row',alignItems:'center',gap:6,paddingHorizontal:12,paddingVertical:7,borderRadius:16,
                  backgroundColor:on?C.green+'22':C.card,borderWidth:1,borderColor:on?C.green:C.border}}>
                <View style={{width:7,height:7,borderRadius:4,backgroundColor:site.online===false?C.red:site.online?C.green:C.sub}}/>
                <Text style={{color:on?C.green:C.text,fontSize:13,fontWeight:on?'700':'500'}}>{site.name}</Text>
              </TouchableOpacity>);})}
          </ScrollView>
        )}
        {cloud&&cloud.sites.length===1&&<Text style={{color:C.muted,fontSize:13,marginTop:6}}>{cloud.sites[0].name}</Text>}
      </View>
      {!!cloud&&!cloud.sites.length&&<AddCameraCard cfg={cloud} onAdded={cfg=>enterCloud(cfg)}/>}

      {setupStatus&&(setupStatus.camera_moved||setupStatus.needs_zones)&&!demoMode&&(
        <TouchableOpacity onPress={()=>setZoneEditor(true)}
          style={{marginHorizontal:16,marginBottom:12,padding:16,borderRadius:14,backgroundColor:C.orange+'22',borderWidth:1,borderColor:C.orange}}>
          <Text style={{color:C.text,fontSize:16,fontWeight:'700'}}>
            {setupStatus.camera_moved?'The camera seems to have moved':'Mark the bed and couch'}
          </Text>
          <Text style={{color:C.sub,fontSize:14,marginTop:4,lineHeight:20}}>
            Tap here to mark them, so lying down there is not mistaken for a fall.
          </Text>
        </TouchableOpacity>
      )}

      {/* Status pills */}
      <View style={s.pillRow}>
        <TouchableOpacity activeOpacity={initError?0.6:1} style={s.pill}
          onPress={()=>{if(initError)Alert.alert('Apple Health is not connected',
            `${initError}\n\nVIGIL's camera monitoring does not depend on it. Apple Health only adds Apple Watch heart rate and sleep.`,
            [{text:'Try again',onPress:()=>startHealthKit()},{text:'OK',style:'cancel'}]);}}>
          <View style={[s.pillDot,{backgroundColor:initialized?C.green:initError?C.orange:C.sub}]}/><Text style={s.pillTxt}>{initialized?'Apple Health':initError?'Health off':'Starting'}</Text></TouchableOpacity>
        <View style={s.pill}><View style={[s.pillDot,{backgroundColor:demoMode||jetsonOk===true?C.green:jetsonOk===false?C.red:C.sub}]}/><Text style={s.pillTxt}>{demoMode?'Demo unit':jetsonOk===true?'Monitoring unit':jetsonOk===false?'Unit offline':'Checking'}</Text></View>
        {jetsonOk===true&&<View style={s.pill}><View style={[s.pillDot,{backgroundColor:syncing?C.orange:C.green}]}/><Text style={s.pillTxt}>{syncing?'Syncing':'Synced'}</Text></View>}
        {cameraStatus?.online&&<View style={s.pill}><View style={[s.pillDot,{backgroundColor:C.green}]}/><Text style={s.pillTxt}>{String(cameraStatus.fps?.toFixed(0)??'?')} fps</Text></View>}
      </View>

      {(()=>{
        // An unacknowledged fall is the first thing on screen, not a list
        // item one tap away.
        const open = alerts.filter(a=>a.alert_type==='fall'&&!a.seen);
        if(open.length===0) return null;
        const a = open[0];
        return (
          <View style={{marginHorizontal:16,marginBottom:12,backgroundColor:C.red+'1F',
                        borderRadius:16,padding:16,borderWidth:1,borderColor:C.red+'66'}}>
            <Text style={{color:C.red,fontSize:12,fontWeight:'800',letterSpacing:0.6}}>
              FALL NOT YET ACKNOWLEDGED
            </Text>
            <Text style={{color:C.text,fontSize:14,marginTop:6,lineHeight:20}}>
              {`Detected ${timeAgo(a.detected_at)}. Someone should check on the resident and confirm here.`}
            </Text>
            <TouchableOpacity onPress={()=>acknowledgeAlert(a.id)}
              style={{backgroundColor:C.red,borderRadius:12,paddingVertical:13,
                      alignItems:'center',marginTop:12}}>
              <Text style={{color:'#fff',fontWeight:'800',fontSize:15}}>
                {open.length>1?`I am checking on them (${open.length} open)`:'I am checking on them'}
              </Text>
            </TouchableOpacity>
          </View>
        );
      })()}

      {/* Demo entry: the only path an App Store reviewer has, since they
          have no Jetson, no camera and no VPN. */}
      {!demoMode&&jetsonOk===false&&(
        <View style={{marginHorizontal:16,marginBottom:12,backgroundColor:C.card,
                      borderRadius:16,padding:16,borderWidth:1,borderColor:C.border}}>
          <Text style={{color:C.text,fontSize:15,fontWeight:'700'}}>No monitoring unit connected</Text>
          <Text style={{color:C.sub,fontSize:13,lineHeight:19,marginTop:6}}>
            VIGIL needs its camera unit on the same network. To see how the app
            works without hardware, open Demo Mode.
          </Text>
          <TouchableOpacity onPress={enterDemo}
            style={{backgroundColor:C.green,borderRadius:12,paddingVertical:12,
                    alignItems:'center',marginTop:14}}>
            <Text style={{color:'#000',fontWeight:'800',fontSize:15}}>Open Demo Mode</Text>
          </TouchableOpacity>
        </View>
      )}

      {/* Room switcher */}
      {rooms.length>1&&(
        <ScrollView horizontal showsHorizontalScrollIndicator={false} contentContainerStyle={{paddingHorizontal:16,paddingBottom:8,gap:8,flexDirection:'row'}}>
          {rooms.map(r=>(
            <TouchableOpacity key={r.id} onPress={()=>setActiveRoomId(r.id)}
              style={{paddingHorizontal:14,paddingVertical:7,borderRadius:20,backgroundColor:activeRoomId===r.id?C.green:C.card,flexDirection:'row',alignItems:'center',gap:6}}>
              <View style={{width:6,height:6,borderRadius:3,backgroundColor:r.camera_online?(activeRoomId===r.id?'#000':C.green):C.muted}}/>
              <Text style={{color:activeRoomId===r.id?'#000':C.text,fontSize:13,fontWeight:'600'}}>{r.name}</Text>
              {(r.person_count??0)>1&&<Text style={{color:activeRoomId===r.id?'#000':C.orange,fontSize:11,fontWeight:'700'}}>{String(r.person_count)}</Text>}
            </TouchableOpacity>
          ))}
        </ScrollView>
      )}

      {/* Camera status */}
      <View style={[s.card,{marginHorizontal:16,marginTop:12,marginBottom:4,borderWidth:1,borderColor:camBorder}]}>
        <View style={{flexDirection:'row',alignItems:'center',gap:10}}>
          <View style={{width:12,height:12,borderRadius:6,backgroundColor:cameraStatus?.online?C.green:C.red}}/>
          <View style={{flex:1}}>
            <Text style={{color:C.text,fontSize:15,fontWeight:'700'}}>
              {cameraStatus?.online&&someoneInView?'Resident Detected':cameraStatus?.online?'Room Empty':'Camera Offline'}
            </Text>
            <Text style={{color:C.sub,fontSize:12,marginTop:2}}>
              {cameraStatus?.online?`${inViewNames?inViewNames+' in view · ':''}${cameraStatus.fps?.toFixed(1)??'?'} fps · Live`:'Monitoring unit offline'}
            </Text>
          </View>
          {(cameraStatus?.person_count??0)>1&&(
            <View style={{backgroundColor:C.orange+'22',borderRadius:8,paddingHorizontal:8,paddingVertical:3}}>
              <Text style={{color:C.orange,fontSize:11,fontWeight:'700'}}>{String(cameraStatus?.person_count)} people</Text>
            </View>
          )}
        </View>
      </View>

      {/* Live camera feed */}
      <Text style={s.secHeader}>Camera Feed</Text>
      <View style={{marginHorizontal:16,marginBottom:4}}>{renderCameraFeed()}</View>
      {!demoMode&&(()=>{
        // IN_VIEW_V1: who the camera recognises right now.
        const inView=inViewList.map(v=>persons.find(p=>p.id===v.id)??({id:v.id,name:v.name,is_resident:v.is_resident,room_id:'',height_px:null,confidence:0,frames_seen:0,first_seen_at:null,last_seen_at:null} as Person));
        return(
          <View style={{marginHorizontal:16,marginTop:12}}>
            <Text style={{color:C.sub,fontSize:13,fontWeight:'700',marginBottom:8}}>In view now <Text style={{color:C.muted,fontWeight:'500'}}>(tap a name to correct it)</Text></Text>
            {inView.length===0&&identifying===0
              ?<Text style={{color:C.muted,fontSize:14}}>No one in view.</Text>
              :<View style={{flexDirection:'row',flexWrap:'wrap',gap:8}}>
                {inView.map(p=>(
                  <TouchableOpacity key={p.id} activeOpacity={0.7}
                    onPress={()=>{
                      // WHO_SHEET_V1: fix a wrong name on the spot, for this exact person on camera.
                      const tr=inViewList.find(v=>v.id===p.id)?.track??null;
                      setWhoToday({track:tr,current:{id:p.id,name:p.name}});
                    }}
                    style={{flexDirection:'row',alignItems:'center',gap:8,paddingLeft:5,paddingRight:14,paddingVertical:5,borderRadius:30,
                    backgroundColor:p.name?C.green+'18':C.card,borderWidth:1.5,borderColor:p.name?C.green+'66':C.border}}>
                    <PersonAvatar personId={p.id} name={p.name} serverUrl={urlRef.current} size={30} isResident={!!p.is_resident}/>
                    <Text style={{color:C.text,fontWeight:'700',fontSize:14}}>{p.name??'New person'}</Text>
                  </TouchableOpacity>
                ))}
                {(inViewOthers.length?inViewOthers:Array.from({length:identifying},()=>({label:'Identifying...',track:null}))).map((o,i)=>(
                  <TouchableOpacity key={'o'+i} activeOpacity={0.7}
                    onPress={()=>{if(o.track!=null)setWhoToday({track:o.track,current:null});}}
                    style={{flexDirection:'row',alignItems:'center',gap:8,paddingHorizontal:14,paddingVertical:10,borderRadius:30,backgroundColor:C.card,borderWidth:1.5,borderColor:o.label==='Visitor'?C.blue+'66':C.border}}>
                    {o.label==='Identifying...'&&<ActivityIndicator size="small" color={C.sub}/>}
                    <Text style={{color:o.label==='Visitor'?C.blue:C.sub,fontWeight:'700',fontSize:14}}>{o.label}</Text>
                  </TouchableOpacity>
                ))}
              </View>}
          </View>
        );
      })()}

      {whoToday&&<WhoIsThisSheet serverUrl={urlRef.current||FALLBACK_URL} track={whoToday.track} current={whoToday.current}
        people={persons} onClose={()=>setWhoToday(null)}
        onDone={(p)=>{setWhoToday(null);setTimeout(()=>fetchPersons(),3500);
          Alert.alert(`Saved: ${p.name}`,'VIGIL will use this right away. For the best recognition, run Introduce everyone for them.');}}/>}
      {/* HOME_PEOPLE_V1: pick whose day to show. The resident is the default. */}
      {namedPeople.length>1&&!demoMode&&(
        <>
          <Text style={s.secHeader}>Whose day</Text>
          <PersonChips people={namedPeople} selectedId={selPid} serverUrl={urlRef.current}
            onPick={p=>{if(!p)return;setHomePid(p.id);}}/>
        </>
      )}

      {/* Activity ring */}
      <Text style={s.secHeader}>{dayName?`${dayName}'s activity`:'Activity'}</Text>
      <Text style={{color:C.muted,fontSize:12,marginHorizontal:20,marginTop:-6,lineHeight:17}}>Share of the time {dayName||'the resident'} was in view today that they were moving. Time out of view doesn't count.</Text>
      {dayLoading
        ?<ActivityIndicator color={C.green} style={{marginVertical:40}}/>
        :<ActivityRing pct={((dayData?.active_minutes??0)+(dayData?.still_minutes??0))>=(selPid?5:20)?(dayData?.activity_level_pct??null):null} activeMin={dayData?.active_minutes??null} stillMin={dayData?.still_minutes??null}/>}

      {/* Camera metrics */}
      <Text style={s.secHeader}>{dayName?`${dayName}'s walking and posture`:'Walking and posture'}</Text>
      <Text style={{color:C.muted,fontSize:12,marginHorizontal:20,marginTop:-6,marginBottom:8,lineHeight:17}}>Typical values today for {dayName||'the resident'} only, from times they walked. Speed is estimated from steps and height.</Text>
      <View style={s.grid}>
        {[
          {label:'Speed (est.)',val:fmt(dayData?.camera_gait_speed_ms,2),unit:'m/s',color:C.green,warn:(dayData?.camera_gait_speed_ms??1)<0.8&&dayData?.camera_gait_speed_ms!=null},
          {label:'Posture',val:fmt(dayData?.posture_score,0),unit:'/100',color:C.purple,warn:(dayData?.posture_score??100)<50&&dayData?.posture_score!=null},
          {label:'Asymmetry',val:fmt(dayData?.camera_asymmetry_pct,1),unit:'%',color:C.orange,warn:(dayData?.camera_asymmetry_pct??0)>15},
          {label:'Cadence',val:fmt(dayData?.camera_cadence_spm,0),unit:'spm',color:C.blue,warn:false},
          {label:'Longest Still',val:fmt(dayData?.longest_stillness_min,0),unit:'min',color:C.orange,warn:(dayData?.longest_stillness_min??0)>90},
          {label:'Data Points',val:fmtLarge(dayData?.camera_data_points),unit:'pts',color:C.sub,warn:false},
        ].map(m=>(
          <View key={m.label} style={s.metCard}>
            <View style={s.metTop}>
              <View style={[s.metDot,{backgroundColor:m.warn?C.red:m.color}]}/>
              <Text style={s.metLbl}>{m.label}</Text>
            </View>
            <Text style={[s.metVal,{color:m.warn?C.red:C.text}]}>{m.val}</Text>
            <Text style={s.metUnit}>{m.unit}</Text>
            {m.warn&&<View style={s.warnChip}><Text style={s.warnChipTxt}>OUTSIDE RANGE</Text></View>}
          </View>
        ))}
      </View>

      {/* Apple Watch — only if enabled and data exists */}
      {watchEnabled&&namedPeople.length>0&&!watchOwnerOk&&!demoMode&&(
        <View style={[s.card,{marginHorizontal:16,marginTop:8}]}>
          <Text style={{color:C.text,fontSize:15,fontWeight:'700'}}>Whose Apple Watch is synced to this phone?</Text>
          <Text style={{color:C.sub,fontSize:13,marginTop:4,marginBottom:10,lineHeight:18}}>Its steps, heart rate and sleep will show only on that person.</Text>
          <PersonChips people={namedPeople} selectedId={null} serverUrl={urlRef.current} onPick={p=>{if(p)setWatchOwner(p.id);}}/>
        </View>
      )}
      {watchHere&&(watchEnabled||demoMode)&&(
        <>
          <Text style={s.secHeader}>{homePerson?`${homePerson.name}'s Apple Watch`:residentName?`${residentName}'s Apple Watch`:'Apple Watch'}</Text>
          {watchOwnerOk&&!demoMode&&<TouchableOpacity onPress={clearWatchOwner}
            style={{marginHorizontal:20,marginTop:-6,marginBottom:6}}><Text style={{color:C.muted,fontSize:12}}>Not {namedPeople.find(p=>p.id===watchOwner)?.name??'their'}'s watch? Tap to change.</Text></TouchableOpacity>}
          {today?.step_count==null&&today?.resting_hr==null&&today?.hrv_sdnn==null&&today?.sleep_hours==null&&(
            <Text style={{color:C.muted,fontSize:13,marginHorizontal:20,marginBottom:8,lineHeight:18}}>
              No watch data yet today. Make sure the watch is paired with this iPhone and VIGIL is allowed in the Health app (Sharing, Apps, VIGIL).
            </Text>
          )}
          <View style={s.grid}>
            {[
              {label:'Steps',val:fmtLarge(today?.step_count),unit:'steps',color:C.green,warn:false},
              {label:'Resting HR',val:fmt(today?.resting_hr,0),unit:'bpm',color:C.blue,warn:false},
              {label:'HRV',val:fmt(today?.hrv_sdnn,1),unit:'ms',color:C.blue,warn:false},
              {label:'Calories',val:fmtLarge(today?.active_calories),unit:'kcal',color:C.orange,warn:false},
              {label:'SpO2',val:fmt(today?.spo2_avg,1),unit:'%',color:C.purple,warn:(today?.spo2_avg??100)<95&&today?.spo2_avg!=null},
              {label:'Sleep',val:fmt(today?.sleep_hours,1),unit:'hrs',color:C.orange,warn:false},
            ].map(m=>(
              <View key={m.label} style={[s.metCard,{borderTopColor:m.warn?C.red:m.color}]}>
                <View style={s.metTop}>
                  <View style={[s.metDot,{backgroundColor:m.warn?C.red:m.color}]}/>
                  <Text style={s.metLbl}>{m.label}</Text>
                </View>
                <Text style={[s.metVal,{color:m.warn?C.red:C.text}]}>{m.val}</Text>
                <Text style={s.metUnit}>{m.unit}</Text>
              </View>
            ))}
          </View>
        </>
      )}
      {lastSync&&<Text style={{color:C.muted,fontSize:11,textAlign:'center',marginTop:12}}>Last synced {lastSync}  ·  +{lastIns??0} new</Text>}
      <View style={{height:32}}/>
    </ScrollView>
  );

  // ─────────────────────────────────────────────
  // TRENDS SCREEN
  // ─────────────────────────────────────────────
  const renderTrends=()=>{
    const t2=new Date();
    const sun=new Date(t2);sun.setDate(t2.getDate()-t2.getDay());sun.setHours(0,0,0,0);
    const weekDates=Array.from({length:7},(_,i)=>{const d=new Date(sun);d.setDate(sun.getDate()+i);return d.toISOString().slice(0,10);});
    const todayStr=new Date().toISOString().slice(0,10);
    const byDate:Record<string,DailySummary>={};history.forEach(r=>{byDate[r.date]=r;});
    const week=(k:keyof DailySummary)=>weekDates.map(d=>{if(d>todayStr)return null;return(byDate[d]?.[k] as number|null)??null;});
    const DAY=['S','M','T','W','T','F','S'];
    // WATCH_OWNER_V2: the watch on this phone belongs to one person; never show it on anyone else.
    // TRENDS_PERSON_V2: with nobody picked, show the resident's own measurements (camera_metrics in the
    // daily summary are an average of everyone in view, so they are only used when nobody is named).
    const shownTrendPerson=demoMode?trendPerson:(trendPerson??residentPerson??namedPeople.find(p=>p.id===watchOwner)??namedPeople[0]??null);
    const watchIsResident=demoMode||(watchEnabled&&(watchOwnerOk?watchOwner===residentPerson?.id:namedPeople.length===0));

    const TrendBar=({data,label,unit,color,dec=0}:{data:(number|null)[];label:string;unit:string;color:string;dec?:number})=>{
      const valid=data.filter(v=>v!=null) as number[];
      if(!valid.length)return null;
      const mean=valid.reduce((a,b)=>a+b,0)/valid.length;
      const std=valid.length>1?Math.sqrt(valid.reduce((a,b)=>a+(b-mean)**2,0)/valid.length):0;
      const min=Math.min(...valid);const max=Math.max(...valid);const range=max-min||1;
      const latest=data.filter(v=>v!=null).slice(-1)[0];
      const BAR=48;const meanBot=Math.max(2,((mean-min)/range)*BAR);
      return(
        <View style={{marginHorizontal:16,marginBottom:10,padding:14,backgroundColor:C.card,borderRadius:16}}>
          <View style={{flexDirection:'row',justifyContent:'space-between',marginBottom:8}}>
            <View>
              <Text style={{color:C.sub,fontSize:12.5,fontWeight:'600',marginBottom:2}}>{label}</Text>
              <Text style={{color,fontSize:22,fontWeight:'800'}}>{latest!=null?latest.toFixed(dec):'—'}<Text style={{color:C.sub,fontSize:12}}> {unit}</Text></Text>
            </View>
            <Text style={{color:C.muted,fontSize:11}}>avg {mean.toFixed(dec)}</Text>
          </View>
          <View style={{height:BAR+8,position:'relative'}}>
            <View style={{position:'absolute',left:0,right:0,bottom:0,top:0,flexDirection:'row',alignItems:'flex-end',gap:3}}>
              {data.map((v,i)=>{
                const isFuture=weekDates[i]>todayStr;
                if(v==null||isFuture)return<View key={i} style={{flex:1,height:3,backgroundColor:C.muted+'20',borderRadius:2}}/>;
                const h=Math.max(4,((v-min)/range)*BAR);
                const isAnom=std>0.001&&Math.abs(v-mean)>1.5*std;
                return(
                  <View key={i} style={{flex:1,height:BAR+8,justifyContent:'flex-end',alignItems:'center'}}>
                    {isAnom?<Text style={{fontSize:9,color:C.red,fontWeight:'800',marginBottom:1}}>!</Text>:<View style={{height:11}}/>}
                    <View style={{width:'100%',height:h,backgroundColor:isAnom?C.red:color,borderRadius:3}}/>
                  </View>
                );
              })}
            </View>
            {valid.length>=2&&<View pointerEvents="none" style={{position:'absolute',left:0,right:0,bottom:meanBot,height:1,borderStyle:'dashed',borderWidth:1,borderColor:C.muted+'AA'}}/>}
          </View>
          <View style={{flexDirection:'row',marginTop:4,gap:3}}>
            {weekDates.map((d,i)=>{
              const isToday=d===todayStr;const isFuture=d>todayStr;
              return<Text key={i} style={{flex:1,textAlign:'center',fontSize:9,color:isToday?color:isFuture?C.muted+'40':C.muted,fontWeight:isToday?'700':'400'}}>{DAY[new Date(d+'T12:00:00').getDay()]}</Text>;
            })}
          </View>
        </View>
      );
    };

    const renderBaseline=()=>{
      const POS_ONLY=['camera_gait_speed_ms','camera_cadence_spm','posture_score','resting_hr','hrv_sdnn','sleep_hours','stride_variability'];
      const ser=(k:keyof DailySummary)=>[...history].sort((a,b)=>a.date<b.date?-1:1).map(r=>{
        const v=r[k] as any;const ok=typeof v==='number'&&isFinite(v)&&!(v<=0&&POS_ONLY.includes(k as string));
        return{date:r.date,v:ok?v:null};});
      const watch=watchIsResident&&history.some(r=>r.resting_hr!=null);
      const ms:BLMeasure[]=[
        {key:'speed',label:'Walking speed',unit:'m/s',dec:2,why:BL_WHY.speed,series:ser('camera_gait_speed_ms')},
        {key:'cadence',label:'Steps per minute',unit:'steps/min',dec:0,why:BL_WHY.cadence,series:ser('camera_cadence_spm')},
        {key:'posture',label:'Posture',unit:'/100',dec:0,why:BL_WHY.posture,series:ser('posture_score')},
        {key:'asym',label:'Step evenness',unit:'% uneven',dec:1,higherWorse:true,why:BL_WHY.asym,series:ser('camera_asymmetry_pct')},
        {key:'stride',label:'Walking steadiness',unit:'% step-time variation',dec:1,higherWorse:true,why:BL_WHY.stride,series:ser('stride_variability')},
        {key:'activity',label:'Time moving',unit:'% of time in view',dec:0,why:BL_WHY.activity,series:ser('activity_level_pct')},
        {key:'still',label:'Longest still period',unit:'min',dec:0,higherWorse:true,why:BL_WHY.still,series:ser('longest_stillness_min')},
        ...(watch?[
          {key:'rhr',label:'Resting heart rate',unit:'bpm',dec:0,higherWorse:true,why:BL_WHY.rhr,series:ser('resting_hr')},
          {key:'hrv',label:'Heart rate variability',unit:'ms',dec:0,why:BL_WHY.hrv,series:ser('hrv_sdnn')},
          {key:'sleep',label:'Sleep',unit:'h',dec:1,why:BL_WHY.sleep,series:ser('sleep_hours')},
        ]:[]),
      ];
      return <BaselineView name={residentPerson?.name??(residentName||'the resident')} measures={ms}
        groups={[{title:'Walking',keys:['speed','cadence','posture','asym','stride']},
                 {title:'Activity',keys:['activity','still']},
                 {title:'Apple Watch',keys:['rhr','hrv','sleep']}]}/>;
    };

    return(
      <ScrollView showsVerticalScrollIndicator={false} automaticallyAdjustKeyboardInsets keyboardDismissMode="interactive" keyboardShouldPersistTaps="handled">
        <View style={s.pageHead}>
          <Text style={s.pageTitle}>Trends</Text>
          <Text style={s.pageSub}>{history.length} days recorded</Text>
        </View>
        <PersonChips people={namedPeople} selectedId={shownTrendPerson?.id??null}
          serverUrl={urlRef.current} onPick={p=>{if(!p)return; setTrendPid(p.id);}}/>
        <Text style={{color:C.muted,fontSize:12,lineHeight:17,marginHorizontal:16,marginBottom:12}}>
          {shownTrendPerson
            ?`${shownTrendPerson.name}'s walking and posture, only from the times VIGIL recognised them${shownTrendPerson.id===watchOwner&&watchEnabled?', plus their Apple Watch':''}. Tap another person to switch.`
            :`These charts are for everyone the camera sees. Name people in Introduce everyone to get each person's own trends.`}
        </Text>
        <View style={{flexDirection:'row',marginHorizontal:16,marginBottom:12,backgroundColor:C.card,borderRadius:12,padding:4}}>
          {([['week','7-Day'],['baseline','Baseline']] as const).map(([key,lbl])=>(
            <TouchableOpacity key={key} onPress={()=>setTrendView(key)}
              style={{flex:1,paddingVertical:9,borderRadius:9,alignItems:'center',
                      backgroundColor:trendView===key?C.green:'transparent'}}>
              <Text style={{color:trendView===key?'#000':C.sub,fontWeight:'700',fontSize:13}}>{lbl}</Text>
            </TouchableOpacity>
          ))}
        </View>
        {watchEnabled&&!watchOwnerOk&&namedPeople.length>0&&!demoMode&&(
          <View style={[s.card,{marginHorizontal:16,marginBottom:12}]}>
            <Text style={{color:C.text,fontSize:15,fontWeight:'700'}}>Whose Apple Watch is synced to this phone?</Text>
            <Text style={{color:C.sub,fontSize:13,marginTop:4,marginBottom:10,lineHeight:18}}>Its heart rate, sleep and steps will show only in that person's trends.</Text>
            <PersonChips people={namedPeople} selectedId={null} serverUrl={urlRef.current} onPick={p=>{if(p)setWatchOwner(p.id);}}/>
          </View>
        )}
        {shownTrendPerson?(
          <>
          <PersonDetailScreen key={shownTrendPerson.id} embedded view={trendView} demoMode={demoMode}
            watchHistory={watchEnabled&&watchOwnerOk&&shownTrendPerson.id===watchOwner?history:null}
            personId={shownTrendPerson.id} personName={shownTrendPerson.name??'This person'}
            serverUrl={urlRef.current} onClose={()=>setTrendPid(null)}/>
          {trendView==='week'&&history.some(r=>r.activity_level_pct!=null||r.active_minutes!=null)&&(
            <>
              <Text style={s.secHeader}>Everyone at home — This Week</Text>
              <Text style={{color:C.muted,fontSize:12,lineHeight:17,marginHorizontal:16,marginTop:-4,marginBottom:8}}>Movement and stillness are measured for the whole room, not one person.</Text>
              <TrendBar data={week('activity_level_pct')} label="Activity Level" unit="%" color={C.green}/>
              <TrendBar data={week('active_minutes')} label="Active Minutes" unit="min" color={C.green}/>
              <TrendBar data={week('longest_stillness_min')} label="Longest Stillness" unit="min" color={C.orange}/>
            </>
          )}
          </>
        ):trendView==='baseline'?renderBaseline():history.length===0?(
          <View style={s.empty}><View style={s.emptyGlyph}><Text style={s.emptyGlyphTxt}>…</Text></View><Text style={s.emptyTxt}>No trend data yet</Text><Text style={s.emptySub}>Data collects automatically each day</Text></View>
        ):(
          <>
            <Text style={s.secHeader}>Activity — This Week</Text>
            <TrendBar data={week('activity_level_pct')} label="Activity Level" unit="%" color={C.green}/>
            <TrendBar data={week('active_minutes')} label="Active Minutes" unit="min" color={C.green}/>
            <TrendBar data={week('longest_stillness_min')} label="Longest Stillness" unit="min" color={C.orange}/>
            <Text style={s.secHeader}>Gait & Posture — This Week</Text>
            <TrendBar data={week('camera_gait_speed_ms')} label="Walking Speed" unit="m/s" color={C.teal} dec={2}/>
            <TrendBar data={week('posture_score')} label="Posture Score" unit="/100" color={C.purple}/>
            <TrendBar data={week('camera_asymmetry_pct')} label="Gait Asymmetry" unit="%" color={C.orange} dec={1}/>
            <TrendBar data={week('stride_variability')} label="Step-Time Variation (lower is steadier)" unit="%" color={C.teal} dec={1}/>
            {watchIsResident&&history.some(r=>r.hrv_sdnn!=null)&&(
              <>
                <Text style={s.secHeader}>{residentPerson?.name?`${residentPerson.name}'s Apple Watch`:'Apple Watch'} — This Week</Text>
                <TrendBar data={week('hrv_sdnn')} label="HRV" unit="ms" color={C.purple} dec={1}/>
                <TrendBar data={week('resting_hr')} label="Resting HR" unit="bpm" color={C.red}/>
              </>
            )}
            <Text style={s.secHeader}>Daily Log</Text>
            <View style={{marginHorizontal:16,backgroundColor:C.card,borderRadius:16,overflow:'hidden',marginBottom:16}}>
              <View style={{flexDirection:'row',paddingHorizontal:12,paddingVertical:10,borderBottomWidth:1,borderBottomColor:C.border}}>
                {['Date','Active','Still','Speed','Posture','Flag'].map(h=><Text key={h} style={{flex:1,color:C.muted,fontSize:9,fontWeight:'700',textTransform:'uppercase'}}>{h}</Text>)}
              </View>
              {history.slice(0,14).map(row=>(
                <View key={row.date} style={{flexDirection:'row',paddingHorizontal:12,paddingVertical:10,borderBottomWidth:1,borderBottomColor:C.fill,backgroundColor:row.anomaly_flag?'#1a0800':'transparent'}}>
                  <Text style={{flex:1,color:C.sub,fontSize:10}}>{row.date?.slice(5)}</Text>
                  <Text style={{flex:1,color:C.sub,fontSize:10}}>{fmt(row.active_minutes,0)}m</Text>
                  <Text style={{flex:1,color:C.sub,fontSize:10}}>{fmt(row.still_minutes,0)}m</Text>
                  <Text style={{flex:1,color:C.sub,fontSize:10}}>{fmt(row.camera_gait_speed_ms,2)}</Text>
                  <Text style={{flex:1,color:C.sub,fontSize:10}}>{fmt(row.posture_score,0)}</Text>
                  <Text style={{flex:1,color:row.anomaly_flag?C.orange:C.green,fontSize:10,fontWeight:'600'}}>{row.anomaly_flag?'Alert':'OK'}</Text>
                </View>
              ))}
            </View>
          </>
        )}
        <View style={{height:32}}/>
      </ScrollView>
    );
  };

  // ─────────────────────────────────────────────
  // ALERTS SCREEN
  // ─────────────────────────────────────────────
  const explainAlert=(a:AlertEntry):string=>{
    const p=a.payload||{};
    if(a.alert_type==='fall')return'A fall was detected: a rapid change from upright to lying down outside a bed or sofa. Check on the resident and confirm below that someone is responding.';
    if(a.alert_type==='bed_sitting')return`Sat up in bed during the night after resting about ${p.rested_min??'?'} minutes, and may be about to stand. A check-in now can prevent a fall.`;
    if(a.alert_type==='bed_exit')return`Got up during the night after resting about ${p.rested_min??'?'} minutes. Many falls happen getting out of bed at night, so a quick check-in may be worthwhile.`;
    if(a.alert_type==='stillness')return`No movement detected for over ${p.duration_min||'?'} minutes. This is outside the normal pattern for this resident.`;
    if(a.alert_type==='health_signal'&&p.metric&&p.value!=null&&p.baseline!=null){
      const pct=p.baseline!==0?Math.round(Math.abs((p.value-p.baseline)/p.baseline)*100):0;
      const dir=p.value>p.baseline?'above':'below';
      // SIGNALS_V2_APP: signals now judge a finished day (p.day)
      const when=p.day?(()=>{try{return 'on '+new Date(p.day+'T12:00:00').toLocaleDateString(undefined,{weekday:'long',month:'short',day:'numeric'});}catch(_e){return 'yesterday';}})():'today';
      return`${p.metric} ${when} (${Number(p.value).toFixed(1)}) was ${pct}% ${dir} this resident's 14-day baseline (${Number(p.baseline).toFixed(1)}).`;
    }
    return p.message??'An alert was triggered by VIGIL.';
  };

  const renderAlerts=()=>(
    <ScrollView showsVerticalScrollIndicator={false} automaticallyAdjustKeyboardInsets keyboardDismissMode="interactive" keyboardShouldPersistTaps="handled">
      <View style={s.pageHead}>
        <Text style={s.pageTitle}>Alerts</Text>
        <Text style={s.pageSub}>{alertCount>0?`${alertCount} unacknowledged`:'All clear'}</Text>
        <Text style={{color:C.muted,fontSize:12,marginTop:4,lineHeight:17}}>
          {`Camera alerts for ${residentPerson?.name??(residentName||'this room')}. Each alert shows who it is for.`}
        </Text>
      </View>
      {alerts.length===0?(
        <View style={s.empty}>
          <View style={s.emptyGlyph}><Text style={s.emptyGlyphTxt}>✓</Text></View>
          <Text style={s.emptyTxt}>No alerts</Text>
          <Text style={s.emptySub}>Falls, stillness events, and health anomalies appear here</Text>
        </View>
      ):alerts.map(a=>{
        const color = a.alert_type==='fall' ? C.red
          : a.alert_type==='stillness'        ? C.orange
          : a.alert_type==='bed_exit'         ? C.purple
          : a.alert_type==='bed_sitting'      ? C.purple
          : a.alert_type==='multiple_persons' ? C.purple
          : a.alert_type==='absence_pattern'  ? C.teal
          : C.blue;
        const badge = a.alert_type==='fall' ? 'FALL DETECTED'
          : a.alert_type==='stillness'        ? 'STILLNESS ALERT'
          : a.alert_type==='bed_exit'         ? 'NIGHT BED EXIT'
          : a.alert_type==='bed_sitting'      ? 'SITTING UP AT NIGHT'
          : a.alert_type==='multiple_persons' ? 'MULTIPLE PEOPLE'
          : a.alert_type==='absence_pattern'  ? 'ABSENCE ALERT'
          : 'HEALTH SIGNAL';
        const isExp=expandedAlert===a.id;
        return(
          <View key={a.id} style={{marginHorizontal:16,marginBottom:10,backgroundColor:C.card,borderRadius:16,padding:16,borderLeftWidth:3,borderLeftColor:color}}>
            <View style={{flexDirection:'row',justifyContent:'space-between',alignItems:'center'}}>
              <View style={{paddingHorizontal:10,paddingVertical:4,borderRadius:8,backgroundColor:color+'22',borderWidth:1,borderColor:color+'55'}}>
                <Text style={{color,fontSize:11,fontWeight:'700',letterSpacing:0.5}}>{badge}</Text>
              </View>
              {!a.seen&&<View style={{width:8,height:8,borderRadius:4,backgroundColor:color}}/>}
            </View>
            <Text style={{color:C.sub,fontSize:11,marginTop:6}}>{timeAgo(a.detected_at)}</Text>
            <Text style={{color:C.muted,fontSize:10,marginTop:1}}>{new Date(a.detected_at).toLocaleString()}</Text>
            {(()=>{
              const subj=namedPeople.find(p=>p.name===a.payload?.resident_name)??residentPerson;
              const nm=subj?.name??a.payload?.resident_name??(residentName||null);
              if(!nm)return null;
              return(
                <View style={{flexDirection:'row',alignItems:'center',gap:8,marginTop:8}}>
                  {subj&&<PersonAvatar personId={subj.id} name={subj.name} serverUrl={urlRef.current} size={26} isResident={!!subj.is_resident}/>}
                  <Text style={{color:C.sub,fontSize:12}}>{`For ${nm}`}</Text>
                </View>
              );
            })()}
            <View style={{marginTop:10,padding:12,backgroundColor:color+'12',borderRadius:10,borderLeftWidth:2,borderLeftColor:color}}>
              <Text style={{color:C.text,fontSize:13,lineHeight:20}}>{explainAlert(a)}</Text>
            </View>
            <View style={{flexDirection:'row',gap:12,marginTop:8}}>
              <TouchableOpacity onPress={()=>setExpandedAlert(isExp?null:a.id)}>
                <Text style={{color:C.muted,fontSize:11}}>{isExp?'Hide details':'Show details'}</Text>
              </TouchableOpacity>
              {a.alert_type==='fall'&&(
                <TouchableOpacity onPress={async()=>{
                  if(demoMode){setReplayMsg(null);setReplayFrames(demoReplay());return;}
                  try{
                    const r=await AX.get(`${url()}/alerts/${a.id}/replay`,{timeout:8000});
                    if(r.data?.available&&Array.isArray(r.data.frames)&&r.data.frames.length>1){setReplayMsg(null);setReplayFrames(r.data.frames);}
                    else{setReplayFrames([]);setReplayMsg('No replay was saved for this fall. Replays are kept for falls detected after this feature was added.');}
                  }catch(_e){setReplayFrames([]);setReplayMsg('Could not load the replay. Check the connection to the monitoring unit.');}
                }}>
                  <Text style={{color:C.red,fontSize:11,fontWeight:'600'}}>Watch replay</Text>
                </TouchableOpacity>
              )}
              {!a.seen&&(
                <TouchableOpacity onPress={()=>acknowledgeAlert(a.id)}>
                  <Text style={{color:C.green,fontSize:11,fontWeight:'600'}}>Acknowledge</Text>
                </TouchableOpacity>
              )}
              {!!a.seen&&!!(a as any).acknowledged_by&&(
                <Text style={{color:C.muted,fontSize:11}}>
                  {`Acknowledged by ${(a as any).acknowledged_by}`}
                </Text>
              )}
            </View>
            {isExp&&(
              <View style={{marginTop:8,gap:3}}>
                {Object.entries(a.payload||{}).map(([k,v])=>(
                  <Text key={k} style={{color:C.sub,fontSize:11}}>{k}: {String(v)}</Text>
                ))}
              </View>
            )}
          </View>
        );
      })}
      <View style={{height:32}}/>
    </ScrollView>
  );

  // ─────────────────────────────────────────────
  // SETTINGS SCREEN
  // ─────────────────────────────────────────────

  const renderResidentTab=()=>{
    const unitBtn=(u:'us'|'metric',label:string)=>(
      <TouchableOpacity onPress={()=>switchUnits(u)} style={{flex:1,paddingVertical:9,borderRadius:9,alignItems:'center',backgroundColor:pUnits===u?C.green+'22':'transparent',borderWidth:1,borderColor:pUnits===u?C.green:C.border}}>
        <Text style={{color:pUnits===u?C.green:C.sub,fontWeight:'700',fontSize:13}}>{label}</Text></TouchableOpacity>);
    const owner=namedPeopleAll.find(p=>p.id===watchOwner);
    return(
    <ScrollView keyboardShouldPersistTaps="handled" automaticallyAdjustKeyboardInsets keyboardDismissMode="interactive">
      <Text style={[s.secHeader,{marginTop:16}]}>Person being watched</Text>
      <View style={s.settGrp}>
        <OField label="Name" value={residentName} onChange={setResidentName} placeholder="Not set"/>
        <View style={{flexDirection:'row',gap:8,paddingHorizontal:16,paddingVertical:12,borderBottomWidth:1,borderBottomColor:C.hair}}>
          {unitBtn('us','US (ft, lb)')}{unitBtn('metric','Metric (cm, kg)')}
        </View>
        {pUnits==='us'?(
          <View style={{flexDirection:'row'}}>
            <View style={{flex:1}}><OField label="Height ft" value={hFt} onChange={setHFt} placeholder="5" keyboard="number-pad"/></View>
            <View style={{flex:1}}><OField label="in" value={hIn} onChange={setHIn} placeholder="6" keyboard="number-pad"/></View>
          </View>
        ):<OField label="Height (cm)" value={hCm} onChange={setHCm} placeholder="168" keyboard="number-pad"/>}
        <Text style={{color:C.muted,fontSize:11,marginHorizontal:16,marginVertical:8,lineHeight:16}}>Height sets their walking speed. Everyone else's height: Settings, Camera, Monitored People, Edit.</Text>
      </View>

      <Text style={s.secHeader}>Alerts</Text>
      <View style={s.settGrp}>
        <OField label="Emergency Phone" value={emergencyPhone} onChange={setEmergencyPhone} placeholder="+1 (206) 555-0000" keyboard="phone-pad" note="Receives a text on a fall or long inactivity."/>
        <OField label="Emergency Email" value={emergencyEmail} onChange={setEmergencyEmail} placeholder="Optional" keyboard="email-address"/>
        <OField label="Your Name" value={responderName}
          onChange={(v:string)=>{setResponderName(v);AsyncStorage.setItem('vigil_caregiver_name',v.trim()).catch(()=>{});}}
          placeholder="e.g. Maria" keyboard="default"
          note="Recorded when you acknowledge an alert, so the record shows who responded."/>
      </View>

      <Text style={s.secHeader}>Apple Watch</Text>
      <View style={s.settGrp}>
        <View style={{flexDirection:'row',alignItems:'center',justifyContent:'space-between',paddingHorizontal:16,paddingVertical:14}}>
          <View style={{flex:1,paddingRight:10}}>
            <Text style={{color:C.text,fontSize:15}}>Show Apple Watch data</Text>
            <Text style={{color:C.muted,fontSize:11,marginTop:2}}>Steps, heart rate and sleep from the watch paired with this phone.</Text>
          </View>
          <Switch value={watchEnabled} onValueChange={setWatchEnabled} trackColor={{false:C.fill,true:C.green}} thumbColor="#FFFFFF"/>
        </View>
        {watchEnabled&&(
          <View style={{paddingTop:4,paddingBottom:6,borderTopWidth:1,borderTopColor:C.hair}}>
            <Text style={{color:C.sub,fontSize:13,marginHorizontal:16,marginTop:10,marginBottom:8}}>
              Whose watch is it? {owner?`Now: ${owner.name}. Tap someone else to change.`:'Tap the person who wears it.'}
            </Text>
            {namedPeopleAll.length?
              <PersonChips people={namedPeopleAll} selectedId={watchOwner} serverUrl={urlRef.current} onPick={p=>{if(p)setWatchOwner(p.id);}}/>
              :<Text style={{color:C.muted,fontSize:12,marginHorizontal:16,marginBottom:8}}>Name people on the camera first.</Text>}
          </View>
        )}
      </View>

      <TouchableOpacity style={[s.btnPrimary,{marginHorizontal:16,marginTop:20,opacity:savingProfile?0.6:1}]} onPress={saveProfile} disabled={savingProfile}>
        {savingProfile?<ActivityIndicator color="#000" size="small"/>:<Text style={s.btnPrimaryTxt}>Save changes</Text>}
      </TouchableOpacity>
      <Text style={{color:C.muted,fontSize:12,textAlign:'center',marginTop:10,marginHorizontal:16,lineHeight:17}}>
        The watch choice saves right away. To rename someone, mark them as a visitor, or remove them: Settings, Camera, Monitored People, Edit.
      </Text>
      <View style={{height:32}}/>
    </ScrollView>
  );};

  const renderServerTab=()=>(
    <ScrollView automaticallyAdjustKeyboardInsets keyboardDismissMode="interactive" keyboardShouldPersistTaps="handled">
      {cloud&&<AccountSection cfg={cloud}/>}
      {cloud&&<AccessSection cfg={cloud} onDeleted={()=>{AsyncStorage.removeItem(SK_CLOUD).catch(()=>{});AsyncStorage.removeItem(SK_ONBOARDING).catch(()=>{});enterCloud(null);setOnboarding(true);}}/>}
      {cloud?(
        <View style={[s.card,{marginHorizontal:16,marginTop:16}]}>
          <Text style={{color:C.text,fontSize:17,fontWeight:'700'}}>Cameras</Text>
          <Text style={{color:C.sub,fontSize:13,marginTop:4}}>Signed in as {cloud.email}</Text>
          {!cloud.sites.length&&<Text style={{color:C.sub,fontSize:14,marginTop:8}}>No cameras yet. Add one with the code on its card.</Text>}
          {cloud.sites.map(site=>(
            <TouchableOpacity key={site.id} onPress={()=>switchSite(site.id)}
              style={{flexDirection:'row',alignItems:'center',paddingVertical:12,borderTopWidth:1,borderTopColor:C.hair}}>
              <View style={{width:9,height:9,borderRadius:5,marginRight:10,backgroundColor:site.online===false?C.red:site.online?C.green:C.sub}}/>
              <View style={{flex:1}}>
                <Text style={{color:C.text,fontSize:15,fontWeight:site.id===cloud.siteId?'700':'400'}}>{site.name}</Text>
                {!!site.home&&site.home!==site.name&&<Text style={{color:C.muted,fontSize:12}}>{site.home}</Text>}
              </View>
              {site.id===cloud.siteId&&<Text style={{color:C.green,fontSize:13,fontWeight:'600'}}>Viewing</Text>}
            </TouchableOpacity>
          ))}
          {!!cloud.siteId&&<TouchableOpacity onPress={()=>setBoxAgain(true)} style={{paddingTop:12}}>
            <Text style={{color:C.blue,fontSize:15}}>New Wi-Fi or camera? Set up the VIGIL box again</Text></TouchableOpacity>}
          <Modal visible={boxAgain} animationType="slide" presentationStyle="fullScreen" onRequestClose={()=>setBoxAgain(false)}>
            {boxAgain&&cloud&&<BoxSetup cfg={cloud} onDone={()=>{setBoxAgain(false);refreshCloudSites();}} onSkip={()=>setBoxAgain(false)}/>}
          </Modal>
          {!!cloud.sites.find(x=>x.id===cloud.siteId)?.ntfy_topic&&(
            <TouchableOpacity onPress={()=>Share.share({message:cloud.sites.find(x=>x.id===cloud.siteId)?.ntfy_topic||''})}>
              <Text style={{color:C.sub,fontSize:12,marginTop:10,lineHeight:17}}>
                Alerts for this camera: subscribe to topic <Text style={{color:C.green}}>{cloud.sites.find(x=>x.id===cloud.siteId)?.ntfy_topic}</Text> in the ntfy app (tap to copy).
              </Text>
            </TouchableOpacity>
          )}
          <TouchableOpacity style={[s.btnPrimary,{marginTop:16,backgroundColor:C.card,borderWidth:1,borderColor:C.border}]}
            onPress={()=>Alert.alert('Sign out of VIGIL Cloud?','The app will go back to your local VIGIL server.',[{text:'Cancel',style:'cancel'},{text:'Sign out',style:'destructive',onPress:()=>{AX.post(`${cloud.gateway}/auth/logout`).catch(()=>{});enterCloud(null);}}])}>
            <Text style={[s.btnPrimaryTxt,{color:C.red}]}>Sign out</Text>
          </TouchableOpacity>
        </View>
      ):(
        <CloudSignIn startMode="signin" onSignedIn={cfg=>enterCloud(cfg)}/>
      )}
      {cloud&&<AddCameraCard cfg={cloud} onAdded={cfg=>enterCloud(cfg)}/>}
      {!cloud&&<Text style={[s.secHeader,{marginTop:20}]}>Local VIGIL server</Text>}
      {!cloud&&<>
      <View style={[s.card,{marginHorizontal:16,marginTop:16,alignItems:'center',paddingVertical:28}]}>
        <View style={{width:16,height:16,borderRadius:8,marginBottom:12,backgroundColor:jetsonOk===true?C.green:jetsonOk===false?C.red:C.sub}}/>
        <Text style={{color:C.text,fontSize:17,fontWeight:'700',marginBottom:4}}>
          {jetsonOk===true?'Connected to VIGIL Server':jetsonOk===false?'Server Unreachable':'Checking...'}
        </Text>
        <Text style={{color:C.sub,fontSize:13}}>{serverIp} · Port {serverPort}</Text>
        {serverVersion&&<Text style={{color:C.muted,fontSize:11,marginTop:4}}>Server {serverVersion}</Text>}
      </View>
      <TouchableOpacity style={[{flexDirection:'row',alignItems:'center',justifyContent:'space-between',paddingHorizontal:16,paddingVertical:14,marginHorizontal:16,marginTop:16,backgroundColor:C.card,borderRadius:12}]} onPress={()=>setAdvancedOpen(p=>!p)}>
        <Text style={{color:C.text,fontSize:15}}>Advanced Connection Settings</Text>
        <Text style={{color:C.sub,fontSize:13}}>{advancedOpen?'Hide':'Show'}</Text>
      </TouchableOpacity>
      {advancedOpen&&(
        <View style={[s.settGrp,{marginTop:8}]}>
          <OField label="IP Address" value={serverIp} onChange={setServerIp} placeholder={DEFAULT_IP} keyboard="numbers-and-punctuation"/>
          <OField label="Port" value={serverPort} onChange={setServerPort} placeholder={DEFAULT_PORT} keyboard="number-pad"/>
          <TouchableOpacity style={[s.btnPrimary,{margin:16}]} onPress={reconnect}><Text style={s.btnPrimaryTxt}>Reconnect</Text></TouchableOpacity>
        </View>
      )}
      </>}
      <View style={{height:32}}/>
    </ScrollView>
  );

  const renderCameraTab=()=>(
    <ScrollView showsVerticalScrollIndicator={false} automaticallyAdjustKeyboardInsets keyboardDismissMode="interactive" keyboardShouldPersistTaps="handled">
      <Text style={s.secHeader}>Camera Status</Text>
      <View style={s.settGrp}>
        <View style={{flexDirection:'row',alignItems:'center',justifyContent:'space-between',paddingHorizontal:16,paddingVertical:14}}>
          <Text style={{color:C.text,fontSize:15}}>Connection</Text>
          <View style={{flexDirection:'row',alignItems:'center',gap:6}}>
            <View style={{width:7,height:7,borderRadius:4,backgroundColor:cameraStatus?.online?C.green:C.red}}/>
            <Text style={{color:cameraStatus?.online?C.green:C.red,fontSize:14,fontWeight:'500'}}>{cameraStatus?.online?'Online':'Offline'}</Text>
          </View>
        </View>
        <View style={{flexDirection:'row',alignItems:'center',justifyContent:'space-between',paddingHorizontal:16,paddingVertical:14,borderTopWidth:1,borderTopColor:C.hair}}>
          <Text style={{color:C.text,fontSize:15}}>Person Detected</Text>
          <Text style={{color:cameraStatus?.person_present?C.green:C.sub,fontSize:14,fontWeight:'500'}}>{cameraStatus?.person_present?'Yes':'No'}</Text>
        </View>
        {(cameraStatus?.person_count??0)>1&&(
          <View style={{flexDirection:'row',alignItems:'center',justifyContent:'space-between',paddingHorizontal:16,paddingVertical:14,borderTopWidth:1,borderTopColor:C.hair}}>
            <Text style={{color:C.text,fontSize:15}}>People in Frame</Text>
            <Text style={{color:C.orange,fontSize:14,fontWeight:'500'}}>{String(cameraStatus?.person_count)} · tracking largest</Text>
          </View>
        )}
        <View style={{flexDirection:'row',alignItems:'center',justifyContent:'space-between',paddingHorizontal:16,paddingVertical:14,borderTopWidth:1,borderTopColor:C.hair}}>
          <Text style={{color:C.text,fontSize:15}}>Frame Rate</Text>
          <Text style={{color:C.sub,fontSize:14,fontWeight:'500'}}>{cameraStatus?.fps!=null?`${cameraStatus.fps.toFixed(1)} fps`:'—'}</Text>
        </View>
      </View>
      <Text style={s.secHeader}>Live View</Text>
      <View style={{marginHorizontal:16}}>{renderCameraFeed()}</View>
      <Text style={{color:C.muted,fontSize:11,marginLeft:16,marginTop:6,marginBottom:4}}>Live view · Tap for full screen</Text>
      <Text style={s.secHeader}>Room setup</Text>
      <TouchableOpacity disabled={demoMode} onPress={()=>setZoneEditor(true)} style={[s.btnPrimary,{marginHorizontal:16,opacity:demoMode?0.4:1}]}>
        <Text style={s.btnPrimaryTxt}>Set up bed and couch zones</Text>
      </TouchableOpacity>
      <Text style={{color:C.muted,fontSize:12,marginHorizontal:16,marginTop:8,lineHeight:18}}>Do this once when the camera is installed, and again if it is moved.</Text>
      <Text style={s.secHeader}>Alerts</Text>
      <TouchableOpacity disabled={demoMode} style={[s.btnPrimary,{marginHorizontal:16,opacity:demoMode?0.4:1,backgroundColor:C.card,borderWidth:1,borderColor:C.border}]}
        onPress={async()=>{   // TEST_ALERT_V1
          try{
            const r=await AX.post(`${url()}/alert/test`,{},{timeout:10000});
            if(r.data?.status==='ok')Alert.alert('Test alert sent','Your phone should buzz within a few seconds. If it does not, open the ntfy app and check you are subscribed to this camera.');
            else Alert.alert('Alerts are not set up',r.data?.reason||'This VIGIL cannot send alerts yet.');
          }catch(e:any){Alert.alert('Could not send',e?.message||'Check the connection and try again.');}
        }}>
        <Text style={[s.btnPrimaryTxt,{color:C.text}]}>Send a test alert</Text>
      </TouchableOpacity>
      <Text style={{color:C.muted,fontSize:12,marginHorizontal:16,marginTop:8,lineHeight:18}}>Every caregiver should do this once, to make sure alerts reach their phone.</Text>
      <Text style={s.secHeader} onLayout={()=>{if(idMode===null&&!demoMode)loadIdMode();}}>Who VIGIL recognises</Text>
      <View style={{flexDirection:'row',marginHorizontal:16,backgroundColor:C.card,borderRadius:14,padding:4,borderWidth:1,borderColor:C.border}}>
        {([['resident','One resident'],['family','Everyone']] as const).map(([k,lbl])=>(
          <TouchableOpacity key={k} disabled={demoMode}
            onPress={()=>{
              if(k==='family'){saveIdMode('family');return;}
              const named=persons.filter(x=>!!x.name);
              if(!named.length){Alert.alert('Name the resident first','Name the resident from Today or Alerts, then choose this.');return;}
              Alert.alert('Who is the resident?','Only this person is named and measured. Everyone else is shown as Visitor.',[
                ...named.slice(0,6).map(x=>({text:x.name as string,onPress:()=>saveIdMode('resident',x.id)})),
                {text:'Cancel',style:'cancel' as const},
              ]);
            }}
            style={{flex:1,paddingVertical:10,borderRadius:10,alignItems:'center',backgroundColor:idMode===k?C.green:'transparent'}}>
            <Text style={{color:idMode===k?'#04140A':C.sub,fontWeight:'700',fontSize:14}}>{lbl}</Text>
          </TouchableOpacity>
        ))}
      </View>
      <Text style={{color:C.muted,fontSize:12,marginHorizontal:16,marginTop:8,lineHeight:18}}>
        {idMode==='resident'
          ?'Only the resident is named and their health measured. Staff, family and visitors appear as Visitor, with no prompts.'
          :'Everyone in the home is named. Tap a name under In view now to confirm or correct it; VIGIL remembers each look.'}
      </Text>
      <Text style={s.secHeader}>Setup Guide</Text>
      {[
        {n:'1',title:'Place the Camera',desc:'Mount the camera 5–7 feet high, angled slightly downward. Position it to see the resident\'s full body when standing in their main living area.'},
        {n:'2',title:'Connect to Wi-Fi',desc:'Open the Tapo or Reolink app, add your camera, and connect it to the same Wi-Fi network as the VIGIL server.'},
        {n:'3',title:'Enable RTSP Stream',desc:'In the camera app, go to Advanced Settings and enable RTSP. Note the camera IP address.'},
        {n:'4',title:'Start Monitoring',desc:'Once the unit is plugged in and connected, monitoring starts on its own. VIGIL spends the first week learning what is normal for the resident.'},
      ].map(step=>(
        <View key={step.n} style={[s.card,{marginHorizontal:16,marginBottom:8,flexDirection:'row',gap:14}]}>
          <View style={{width:28,height:28,borderRadius:14,backgroundColor:C.green+'22',alignItems:'center',justifyContent:'center'}}>
            <Text style={{color:C.green,fontWeight:'700',fontSize:13}}>{step.n}</Text>
          </View>
          <View style={{flex:1}}>
            <Text style={{color:C.text,fontSize:15,fontWeight:'600',marginBottom:4}}>{step.title}</Text>
            <Text style={{color:C.sub,fontSize:13,lineHeight:19}}>{step.desc}</Text>
          </View>
        </View>
      ))}
      {persons.length>0&&(
        <>
          <Text style={s.secHeader}>Monitored People</Text>
          <TouchableOpacity onPress={()=>setShowTeach(true)}
            style={[s.card,{marginHorizontal:16,marginBottom:10,flexDirection:'row',alignItems:'center',borderWidth:1,borderColor:C.green+'55'}]}>
            <View style={{flex:1}}>
              <Text style={{color:C.text,fontSize:15,fontWeight:'700'}}>Introduce everyone</Text>
              <Text style={{color:C.sub,fontSize:12,marginTop:3,lineHeight:17}}>Teach VIGIL who is who, one person at a time. Do it again whenever names are often wrong.</Text>
            </View>
            <Text style={{color:C.green,fontSize:15,fontWeight:'700'}}>Start</Text>
          </TouchableOpacity>
          <Text style={{color:C.muted,fontSize:12,marginHorizontal:16,marginBottom:8,lineHeight:17}}>
            Everyone VIGIL has recognised. Tap Edit to rename, set their height, mark as a visitor, merge a duplicate or delete. Press and hold anyone to delete them.
          </Text>
          {persons.map(p=>(
            <TouchableOpacity
              key={p.id}
              activeOpacity={p.name?0.6:1}
              onPress={()=>{if(p.name)setSelectedPerson(p);}}
              onLongPress={()=>deletePerson(p)}
              style={[s.card,{marginHorizontal:16,marginBottom:8,flexDirection:'row',alignItems:'center',justifyContent:'space-between'}]}>
              <View style={{flexDirection:'row',alignItems:'center',gap:10,flex:1,minWidth:0}}>
                <PersonAvatar personId={p.id} name={p.name} serverUrl={urlRef.current} size={48} isResident={!!p.is_resident}/>
                <View style={{flex:1,minWidth:0}}>
                  <Text style={{color:C.text,fontSize:14,fontWeight:'600'}} numberOfLines={1} ellipsizeMode="tail">{p.name??'Unidentified Person'}</Text>
                  <Text style={{color:C.sub,fontSize:11,marginTop:2}} numberOfLines={1} ellipsizeMode="tail">
                    {p.is_resident?'Resident':'Visitor'}{p.height_cm?` · ${fmtHeight(p.height_cm,pUnits)}`:''}{p.name?' · Tap for trends':''}
                  </Text>
                </View>
              </View>
              {!p.name?(
                <TouchableOpacity
                  style={{backgroundColor:C.green,paddingHorizontal:12,paddingVertical:6,borderRadius:8}}
                  onPress={()=>{
                    setPendingPersonAlert({id:0,alert_type:'manual',room_id:'room_1',person_ids:[p.id],resolved:0,created_at:''});
                    setShowPersonModal(true);
                  }}>
                  <Text style={{color:'#000',fontSize:12,fontWeight:'600'}}>Identify</Text>
                </TouchableOpacity>
              ):(
                <View style={{flexDirection:'row',alignItems:'center',gap:12}}>
                  <TouchableOpacity hitSlop={{top:10,bottom:10,left:10,right:10}} onPress={()=>editPerson(p)}>
                    <Text style={{color:C.blue,fontSize:13,fontWeight:'700'}}>Edit</Text>
                  </TouchableOpacity>
                  <Text style={{color:C.muted,fontSize:16}}>{'>'}</Text>
                </View>
              )}
            </TouchableOpacity>
          ))}
        </>
      )}
      {rooms.length>0&&(
        <>
          <Text style={s.secHeader}>Active Rooms ({String(rooms.length)})</Text>
          {rooms.map(r=>(
            <View key={r.id} style={[s.card,{marginHorizontal:16,marginBottom:8,flexDirection:'row',alignItems:'center',justifyContent:'space-between'}]}>
              <View style={{flexDirection:'row',alignItems:'center',gap:10}}>
                <View style={{width:8,height:8,borderRadius:4,backgroundColor:r.camera_online?C.green:C.muted}}/>
                <View>
                  <Text style={{color:C.text,fontSize:14,fontWeight:'600'}}>{r.name}</Text>
                  {r.resident_name&&<Text style={{color:C.sub,fontSize:11,marginTop:2}}>{r.resident_name}</Text>}
                </View>
              </View>
              <Text style={{color:r.camera_online?C.green:C.muted,fontSize:12}}>{r.camera_online?`${r.fps?.toFixed(0)??'?'} fps`:'Offline'}</Text>
            </View>
          ))}
        </>
      )}
      <View style={{height:32}}/>
    </ScrollView>
  );

  const renderSupportTab=()=>(
    <ScrollView automaticallyAdjustKeyboardInsets keyboardDismissMode="interactive" keyboardShouldPersistTaps="handled">
      <Text style={s.secHeader}>App Info</Text>
      <View style={s.settGrp}>
        <View style={{flexDirection:'row',justifyContent:'space-between',paddingHorizontal:16,paddingVertical:14}}><Text style={{color:C.text,fontSize:15}}>App Version</Text><Text style={{color:C.sub,fontSize:14,fontWeight:'500'}}>VIGIL v1.0.0</Text></View>
        {serverVersion&&<View style={{flexDirection:'row',justifyContent:'space-between',paddingHorizontal:16,paddingVertical:14,borderTopWidth:1,borderTopColor:C.hair}}><Text style={{color:C.text,fontSize:15}}>Server Version</Text><Text style={{color:C.sub,fontSize:14,fontWeight:'500'}}>{serverVersion}</Text></View>}
        <View style={{flexDirection:'row',justifyContent:'space-between',paddingHorizontal:16,paddingVertical:14,borderTopWidth:1,borderTopColor:C.hair}}><Text style={{color:C.text,fontSize:15}}>HealthKit</Text><Text style={{color:initialized?C.green:initError?C.orange:C.sub,fontSize:14,fontWeight:'500'}}>{initialized?'Connected':initError?'Not connected':'Starting'}</Text></View>
        <View style={{flexDirection:'row',justifyContent:'space-between',paddingHorizontal:16,paddingVertical:14,borderTopWidth:1,borderTopColor:C.hair}}><Text style={{color:C.text,fontSize:15}}>Active Rooms</Text><Text style={{color:C.sub,fontSize:14,fontWeight:'500'}}>{String(rooms.length||1)}</Text></View>
      </View>
      <Text style={s.secHeader}>Sync Apple Watch</Text>
      <View style={[s.card,{marginHorizontal:16}]}>
        <TouchableOpacity style={[s.btnPrimary,{opacity:syncing||!initialized||!jetsonOk?0.5:1}]} onPress={()=>performSync(false)} disabled={syncing||!initialized||jetsonOk!==true}>
          {syncing?<View style={{flexDirection:'row',alignItems:'center',justifyContent:'center',gap:8}}><ActivityIndicator color="#000" size="small"/><Text style={s.btnPrimaryTxt}>Syncing...</Text></View>:<Text style={s.btnPrimaryTxt}>Sync Apple Watch Now</Text>}
        </TouchableOpacity>
        {lastSync&&<Text style={{color:C.muted,fontSize:11,textAlign:'center',marginTop:8}}>Last synced {lastSync}  ·  +{lastIns??0} new  ·  {lastSkip??0} skipped</Text>}
      </View>
      <Text style={s.secHeader}>Diagnostics</Text>
      <View style={s.settGrp}>
        <TouchableOpacity style={{flexDirection:'row',justifyContent:'space-between',paddingHorizontal:16,paddingVertical:14}} onPress={()=>setShowLogs(p=>!p)}>
          <Text style={{color:C.text,fontSize:15}}>Debug Logs</Text>
          <Text style={{color:C.sub,fontSize:13}}>{showLogs?'Hide':'Show ('+debugLog.length+')'}</Text>
        </TouchableOpacity>
        {showLogs&&(
          <View style={{padding:12}}>
            <ScrollView style={{maxHeight:280,backgroundColor:'#000',borderRadius:8,padding:8}} nestedScrollEnabled>
              {debugLog.length===0?<Text style={{color:C.muted,fontSize:11}}>No log entries.</Text>:debugLog.map((e,i)=>(
                <Text key={i} style={{fontSize:10,fontFamily:'Courier New',lineHeight:16,color:e.level==='ERROR'?C.red:e.level==='WARN'?C.orange:e.level==='OK'?C.green:C.sub}}>{e.ts}  [{e.level}]  {e.msg}</Text>
              ))}
            </ScrollView>
            <TouchableOpacity style={[s.btnSecondary,{marginTop:8}]} onPress={()=>setDebugLog([])}><Text style={s.btnSecondaryTxt}>Clear Logs</Text></TouchableOpacity>
          </View>
        )}
        <TouchableOpacity style={{flexDirection:'row',justifyContent:'space-between',paddingHorizontal:16,paddingVertical:14,borderTopWidth:1,borderTopColor:C.hair}}
          onPress={()=>Alert.alert('Reset Onboarding','This will delete all saved settings and restart the setup flow.',
            [{text:'Cancel',style:'cancel'},{text:'Reset',style:'destructive',onPress:async()=>{await (AsyncStorage as any).multiRemove([SK_ONBOARDING,SK_PROFILE,SK_SERVER]);setOnboarding(true);}}])}>
          <Text style={{color:C.red,fontSize:15}}>Reset Onboarding</Text>
          <Text style={{color:C.muted,fontSize:13}}>Restart setup</Text>
        </TouchableOpacity>
      </View>
      <View style={{height:32}}/>
    </ScrollView>
  );

  const renderSettings=()=>(
    <View style={{flex:1}}>
      <View style={s.pageHead}><Text style={s.pageTitle}>Settings</Text></View>
      <View style={{flexDirection:'row',borderBottomWidth:1,borderBottomColor:C.border,paddingHorizontal:16}}>
        {(['resident','server','camera','support'] as SettingTab[]).map(t=>(
          <TouchableOpacity key={t} style={{flex:1,alignItems:'center',paddingVertical:10}} onPress={()=>setSettingsTab(t)}>
            <Text style={{color:settingsTab===t?C.green:C.sub,fontSize:14,fontWeight:'600'}}>{({resident:'Person',server:'Account',camera:'Camera',support:'Help'} as any)[t]||t}</Text>
            {settingsTab===t&&<View style={{position:'absolute',bottom:0,left:0,right:0,height:2,backgroundColor:C.green}}/>}
          </TouchableOpacity>
        ))}
      </View>
      <View style={{flex:1}}>
        {settingsTab==='resident'&&renderResidentTab()}
        {settingsTab==='server'&&renderServerTab()}
        {settingsTab==='camera'&&renderCameraTab()}
        {settingsTab==='support'&&renderSupportTab()}
      </View>
    </View>
  );

  // ─────────────────────────────────────────────
  // BOOT / ONBOARDING STATES
  // ─────────────────────────────────────────────
  if(booting){
    return(
      <View style={{flex:1,backgroundColor:C.bg,alignItems:'center',justifyContent:'center'}}>
        <StatusBar barStyle="light-content"/>
        <Text style={{color:C.green,fontSize:42,fontWeight:'800',letterSpacing:4}}>VIGIL</Text>
        <Text style={{color:C.muted,fontSize:13,marginTop:8,letterSpacing:2}}>HEALTH MONITOR</Text>
        <ActivityIndicator color={C.green} style={{marginTop:32}}/>
      </View>
    );
  }

  if(showOnboarding){
    return(
      <OnboardingScreen pendingCode={pendingCode} onDemo={()=>{setOnboarding(false);enterDemo();}}
        onCloud={cfg=>{enterCloud(cfg);setOnboarding(false);setPendingCode(null);AsyncStorage.removeItem('vigil_pending_code').catch(()=>{});
          // HOME_SETUP_V1: pick up the profile and watch choice the setup just saved
          Promise.all([AsyncStorage.getItem(SK_PROFILE),AsyncStorage.getItem(SK_WATCH_OWNER)]).then(([ps,wo])=>{
            setWatchOwnerS(wo||null);
            if(ps){try{const p:SavedProfile=JSON.parse(ps);setResidentName(p.residentName||'');setWatchEnabled(!!p.watchEnabled);
              if(p.units)setPUnits(p.units==='metric'?'metric':'us');}catch(_e){}}
          }).catch(()=>{});
          AsyncStorage.setItem('vigil_teach_offered_'+cfg.siteId,'1').catch(()=>{});}}
        onComplete={(profile,ip,port)=>{
        setResidentName(profile.residentName);setResidentHeight(profile.residentHeight);
        setEmergencyPhone(profile.emergencyPhone);setEmergencyEmail(profile.emergencyEmail);
        setWatchEnabled(profile.watchEnabled);
        setServerIp(ip);setServerPort(port);
        const u=`http://${ip}:${port}`;urlRef.current=u;
        setOnboarding(false);
      }}/>
    );
  }

  // ─────────────────────────────────────────────
  // MAIN RENDER
  // ─────────────────────────────────────────────
  const TABS:[MainTab,string,string][]=[
    ['today','Today','⊙'],
    ['trends','Trends','↗'],
    ['alerts','Alerts','⚑'],
    ['insights','AI','◈'],
    ['settings','Settings','⋯'],
  ];

  return(
    <SafeAreaView style={s.root}>
      <StatusBar barStyle="light-content"/>

      {/* Fullscreen camera modal */}
      <Modal visible={fullscreen} animationType="fade" statusBarTranslucent>
        <View style={{flex:1,backgroundColor:'#000',alignItems:'center',justifyContent:'center'}}>
          <StatusBar barStyle="light-content" backgroundColor="#000"/>
          {cameraFrame?(
            <Image source={{uri:`data:image/jpeg;base64,${cameraFrame}`}} style={{width:SW,height:SW*9/16}} resizeMode="contain" fadeDuration={0}/>
          ):(
            <Text style={{color:C.muted,fontSize:16}}>No camera feed</Text>
          )}
          <TouchableOpacity
            style={{position:'absolute',top:56,right:20,backgroundColor:'rgba(0,0,0,0.7)',borderRadius:20,paddingHorizontal:16,paddingVertical:8}}
            onPress={()=>setFullscreen(false)}>
            <Text style={{color:'#fff',fontSize:14,fontWeight:'700'}}>Close</Text>
          </TouchableOpacity>
          {cameraFrame&&(
            <View style={{position:'absolute',top:60,left:20,backgroundColor:'rgba(0,0,0,0.7)',borderRadius:8,flexDirection:'row',alignItems:'center',paddingHorizontal:10,paddingVertical:5,gap:6}}>
              <Animated.View style={{width:6,height:6,borderRadius:3,backgroundColor:C.green,transform:[{scale:LIVE_PULSE}]}}/>
              <Text style={{color:'#fff',fontSize:12,fontWeight:'700',letterSpacing:1}}>LIVE</Text>
            </View>
          )}
          <Text style={{position:'absolute',bottom:48,color:C.muted,fontSize:12}}>
            {someoneInView?'Resident Detected':'Room Empty'} · {cameraStatus?.fps?.toFixed(1)??'?'} fps
          </Text>
        </View>
      </Modal>

      <Modal visible={replayFrames!==null} animationType="slide" transparent statusBarTranslucent onRequestClose={()=>setReplayFrames(null)}>
        <View style={{flex:1,backgroundColor:'rgba(0,0,0,0.85)',justifyContent:'flex-end'}}>
          <View style={{backgroundColor:C.card,borderTopLeftRadius:24,borderTopRightRadius:24,padding:24,paddingBottom:44}}>
            <View style={s.sheetHandle}/>
            <Text style={{color:C.text,fontSize:20,fontWeight:'800',marginBottom:4}}>Fall replay</Text>
            <Text style={{color:C.sub,fontSize:13,lineHeight:19,marginBottom:14}}>
              {demoMode?'Sample data. ':''}The 30 seconds before the alert, as a stick figure. VIGIL never stores video.
            </Text>
            {replayFrames&&replayFrames.length>1?<FallReplayPlayer frames={replayFrames}/>:
              <Text style={{color:C.sub,fontSize:14,lineHeight:20}}>{replayMsg??''}</Text>}
            <TouchableOpacity onPress={()=>setReplayFrames(null)} style={{marginTop:18,paddingVertical:13,borderRadius:12,alignItems:'center',borderWidth:1,borderColor:C.border}}>
              <Text style={{color:C.sub,fontWeight:'600',fontSize:14}}>Close</Text>
            </TouchableOpacity>
          </View>
        </View>
      </Modal>

      {/* NEW_PERSON_SHEET_V2: one sheet per new person, photo first, name field
          already open. Wrapped in KeyboardAvoidingView so the keyboard pushes
          the sheet up instead of covering what is being typed. */}
      <Modal visible={showPersonModal && !!pendingPersonAlert} animationType="slide" transparent statusBarTranslucent
        onRequestClose={()=>{setShowPersonModal(false);setPendingPersonAlert(null);}}>
        <KeyboardAvoidingView style={{flex:1}} behavior={Platform.OS==='ios'?'padding':'height'}>
          <View style={{flex:1,backgroundColor:'rgba(0,0,0,0.85)',justifyContent:'flex-end'}}>
            <View style={{backgroundColor:C.card,borderTopLeftRadius:24,borderTopRightRadius:24,maxHeight:'92%'}}>
              <ScrollView keyboardShouldPersistTaps="handled" keyboardDismissMode="interactive"
                contentContainerStyle={{padding:24,paddingBottom:40}} showsVerticalScrollIndicator={false}>
                <View style={s.sheetHandle}/>
                <Text style={{color:C.text,fontSize:22,fontWeight:'800',marginBottom:6}}>New person detected</Text>
                <Text style={{color:C.sub,fontSize:14,lineHeight:20,marginBottom:18}}>
                  Who is this? Naming them once is enough; VIGIL will not ask about them again.
                </Text>
                {(pendingPersonAlert?.person_ids??[])
                  .filter(pid=>!persons.find(p=>p.id===pid&&!!p.name))
                  .slice(0,1)
                  .map(pid=>{
                  const known=persons.filter(p=>p.name&&p.name!=='Visitor'&&p.id!==pid);
                  const save=(v?:string)=>{const n=((v??nameRef.current)||newPersonName).trim();if(n){nameRef.current='';identifyPerson(pid,n,isResidentFlag,pendingPersonAlert?.id);}};
                  return(
                    <View key={pid}>
                      <PersonPhotoCard personId={pid} serverUrl={urlRef.current} large/>
                      <KInput
                        style={{backgroundColor:C.bg,color:C.text,fontSize:18,paddingHorizontal:16,paddingVertical:14,
                                borderRadius:14,borderWidth:1.5,borderColor:C.green+'66',marginTop:6}}
                        placeholder="Their name" placeholderTextColor={C.muted}
                        value={newPersonName} onChange={setNewPersonName} liveRef={nameRef}
                        autoCapitalize="words" autoCorrect={false} returnKeyType="done"
                        onSubmit={(v:string)=>save(v)}/>
                      <View style={{flexDirection:'row',gap:10,alignItems:'center',marginTop:12}}>
                        <Switch value={isResidentFlag} onValueChange={setIsResidentFlag} trackColor={{false:C.fill,true:C.green}} thumbColor="#FFF"/>
                        <Text style={{color:C.sub,fontSize:13,flex:1}}>This is the resident being monitored</Text>
                      </View>
                      <TouchableOpacity onPress={()=>save()} disabled={newPersonName.trim().length===0&&!nameRef.current.trim()}
                        style={{backgroundColor:C.green,borderRadius:12,paddingVertical:14,alignItems:'center',marginTop:14,
                                opacity:newPersonName.trim().length>0?1:0.45}}>
                        <Text style={{color:'#000',fontWeight:'800',fontSize:15}}>Save name</Text>
                      </TouchableOpacity>
                      {known.length>0&&(
                        <View style={{marginTop:18}}>
                          <Text style={{color:C.muted,fontSize:12,marginBottom:8}}>Or, if this is someone VIGIL already knows:</Text>
                          <View style={{flexDirection:'row',gap:8,flexWrap:'wrap'}}>
                            {known.map(p=>(
                              <TouchableOpacity key={p.id} onPress={()=>mergePerson(pid,p.id,pendingPersonAlert?.id)}
                                style={{backgroundColor:C.fill,paddingHorizontal:12,paddingVertical:8,borderRadius:10,borderWidth:1,borderColor:C.border}}>
                                <Text style={{color:C.text,fontSize:13,fontWeight:'600'}}>{`Same as ${p.name}`}</Text>
                              </TouchableOpacity>
                            ))}
                          </View>
                        </View>
                      )}
                      <View style={{flexDirection:'row',gap:10,marginTop:18}}>
                        <TouchableOpacity onPress={()=>identifyPerson(pid,'Visitor',false,pendingPersonAlert?.id)}
                          style={{flex:1,paddingVertical:12,borderRadius:12,alignItems:'center',borderWidth:1,borderColor:C.border}}>
                          <Text style={{color:C.sub,fontWeight:'600',fontSize:14}}>Visitor</Text>
                        </TouchableOpacity>
                        <TouchableOpacity
                          onPress={()=>{
                            if(pendingPersonAlert?.id)snoozedRef.current[pendingPersonAlert.id]=Date.now()+30*60*1000;
                            setShowPersonModal(false);setPendingPersonAlert(null);setNewPersonName('');
                          }}
                          style={{flex:1,paddingVertical:12,borderRadius:12,alignItems:'center',borderWidth:1,borderColor:C.border}}>
                          <Text style={{color:C.sub,fontWeight:'600',fontSize:14}}>Not now</Text>
                        </TouchableOpacity>
                      </View>
                    </View>
                  );
                })}
                {(pendingPersonAlert?.person_ids??[]).every(pid=>!!persons.find(p=>p.id===pid&&!!p.name))&&(
                  <TouchableOpacity onPress={()=>{setShowPersonModal(false);setPendingPersonAlert(null);
                      if(pendingPersonAlert?.id)AX.post(`${urlRef.current||FALLBACK_URL}/persons/alerts/${pendingPersonAlert.id}/resolve`,{}).catch(()=>{});}}
                    style={{padding:14,alignItems:'center'}}>
                    <Text style={{color:C.sub,fontSize:14}}>Already identified. Close</Text>
                  </TouchableOpacity>
                )}
              </ScrollView>
            </View>
          </View>
        </KeyboardAvoidingView>
      </Modal>

      <Modal visible={zoneEditor} animationType="slide" presentationStyle="fullScreen" onRequestClose={()=>setZoneEditor(false)}>
        {zoneEditor&&<ZoneEditor serverUrl={urlRef.current} onClose={()=>{setZoneEditor(false);setupAsked.current='';setTimeout(()=>fetchSetupStatus(),800);}}/>}
      </Modal>

      {/* Person detail screen */}
      <Modal visible={showTeach} animationType="slide" presentationStyle="fullScreen" onRequestClose={()=>setShowTeach(false)}>
        {showTeach&&<TeachWhoScreen serverUrl={urlRef.current||FALLBACK_URL} units={pUnits} onClose={()=>{setShowTeach(false);fetchPersons();}}/>}
      </Modal>
      <Modal visible={!!selectedPerson} animationType="slide" presentationStyle="fullScreen">
        {selectedPerson&&(
          <PersonDetailScreen demoMode={demoMode}
            personId={selectedPerson.id}
            personName={selectedPerson.name??'Unidentified Person'}
            serverUrl={urlRef.current}
            onClose={()=>setSelectedPerson(null)}
          />
        )}
      </Modal>

      {/* Main app */}
      <View style={{flex:1}}>
        {demoMode&&(
          <View style={{flexDirection:'row',alignItems:'center',justifyContent:'space-between',
                        backgroundColor:C.orange+'22',borderBottomWidth:1,
                        borderBottomColor:C.orange+'55',paddingHorizontal:16,paddingVertical:8}}>
            <Text style={{color:C.orange,fontSize:11,fontWeight:'700',flex:1,letterSpacing:0.3}}>
              DEMO MODE — sample data, not a live system
            </Text>
            <TouchableOpacity onPress={exitDemo}
              style={{paddingHorizontal:10,paddingVertical:4,borderRadius:8,
                      borderWidth:1,borderColor:C.orange+'77'}}>
              <Text style={{color:C.orange,fontSize:11,fontWeight:'700'}}>Exit</Text>
            </TouchableOpacity>
          </View>
        )}
        {activeTab==='today'&&renderToday()}
        {activeTab==='trends'&&renderTrends()}
        {activeTab==='alerts'&&renderAlerts()}
        {activeTab==='insights'&&<AIInsightsScreen serverUrl={urlRef.current} demoMode={demoMode} people={persons}/>}
        {activeTab==='settings'&&renderSettings()}
      </View>

      {/* Tab bar */}
      <View style={s.tabBar}>
        {TABS.map(([key,label,icon])=>{
          const active=activeTab===key; const color=active?C.green:C.muted;
          return(
            <TouchableOpacity key={key} style={{flex:1,alignItems:'center',paddingTop:4,paddingBottom:2}} onPress={()=>setActiveTab(key)}>
              {active&&<View style={s.tabIndicator}/>}
              <View style={{position:'relative'}}>
                <TabIcon name={key} color={color}/>
                {key==='alerts'&&alertCount>0&&(
                  <View style={s.badge}><Text style={s.badgeTxt}>{alertCount>9?'9+':String(alertCount)}</Text></View>
                )}
              </View>
              <Text style={{fontSize:11,color,fontWeight:active?'700':'600',marginTop:3}}>{label}</Text>
            </TouchableOpacity>
          );
        })}
      </View>
    </SafeAreaView>
  );
}

// ─────────────────────────────────────────────
// STYLES
// ─────────────────────────────────────────────
const s=StyleSheet.create({
  root:         {flex:1,backgroundColor:C.bg},
  pageHead:     {paddingHorizontal:20,paddingTop:20,paddingBottom:12},
  pageTitle:    {color:C.text,fontSize:36,fontWeight:'800',letterSpacing:-1.1},
  pageSub:      {color:C.sub,fontSize:15,marginTop:4},
  secHeader:    {color:C.text,fontSize:21,fontWeight:'800',letterSpacing:-0.5,marginLeft:20,marginTop:30,marginBottom:12},
  pillRow:      {flexDirection:'row',paddingHorizontal:16,gap:8,marginBottom:4,flexWrap:'wrap'},
  pill:         {flexDirection:'row',alignItems:'center',backgroundColor:'rgba(50,215,75,0.07)',borderRadius:100,paddingHorizontal:12,paddingVertical:7,gap:7,borderWidth:1,borderColor:'rgba(50,215,75,0.22)'},
  pillDot:      {width:7,height:7,borderRadius:4},
  pillTxt:      {color:'#CFEFD6',fontSize:12.5,fontWeight:'700'},
  card:         {backgroundColor:C.card,borderRadius:20,padding:18,borderWidth:1,borderColor:C.border},
  grid:         {flexDirection:'row',flexWrap:'wrap',paddingHorizontal:12,gap:10},
  metCard:      {width:(SW-44)/2,backgroundColor:C.card,borderRadius:20,padding:16,borderWidth:1,borderColor:C.border},
  metTop:       {flexDirection:'row',alignItems:'center',gap:6,marginBottom:10},
  metDot:       {width:7,height:7,borderRadius:4},
  metLbl:       {color:C.sub,fontSize:12.5,fontWeight:'600'},
  metVal:       {color:C.text,fontSize:28,fontWeight:'800',letterSpacing:-0.8,fontVariant:['tabular-nums']},
  metUnit:      {color:C.sub,fontSize:11,marginTop:3},
  warnChip:     {alignSelf:'flex-start',marginTop:8,backgroundColor:'rgba(255,69,58,0.14)',borderRadius:100,paddingHorizontal:8,paddingVertical:3},
  warnChipTxt:  {color:C.red,fontSize:9,fontWeight:'700',letterSpacing:0.4},
  empty:        {alignItems:'center',paddingVertical:56,paddingHorizontal:32},
  emptyGlyph:   {width:64,height:64,borderRadius:20,backgroundColor:'rgba(50,215,75,0.12)',alignItems:'center',justifyContent:'center',marginBottom:16},
  emptyGlyphTxt:{color:C.green,fontSize:24,fontWeight:'800'},
  emptyTxt:     {color:C.text,fontSize:16,fontWeight:'600'},
  emptySub:     {color:C.muted,fontSize:13,textAlign:'center',marginTop:5,lineHeight:19},
  settGrp:      {marginHorizontal:16,backgroundColor:C.card,borderRadius:20,overflow:'hidden',borderWidth:1,borderColor:C.border},
  btnPrimary:   {backgroundColor:C.green,borderRadius:100,paddingVertical:16,paddingHorizontal:20,alignItems:'center',shadowColor:C.green,shadowOpacity:0.4,shadowRadius:16,shadowOffset:{width:0,height:6}},
  btnPrimaryTxt:{color:'#04140A',fontSize:16,fontWeight:'800'},
  btnSecondary: {backgroundColor:'rgba(255,255,255,0.04)',borderRadius:100,paddingVertical:14,paddingHorizontal:20,alignItems:'center',borderWidth:1,borderColor:C.border},
  btnSecondaryTxt:{color:C.text,fontSize:14,fontWeight:'600'},
  tabBar:       {flexDirection:'row',backgroundColor:'#070B09',borderTopWidth:1,borderTopColor:C.hair,paddingBottom:22,paddingTop:10},
  tabIndicator: {position:'absolute',top:-10,alignSelf:'center',width:28,height:3,borderRadius:2,backgroundColor:C.green,shadowColor:C.green,shadowOpacity:0.8,shadowRadius:6,shadowOffset:{width:0,height:0}},
  badge:        {position:'absolute',top:-3,right:-8,backgroundColor:C.red,borderRadius:7,minWidth:15,height:15,alignItems:'center',justifyContent:'center',paddingHorizontal:3,borderWidth:1.5,borderColor:'#070B09'},
  badgeTxt:     {color:'#fff',fontSize:9,fontWeight:'800'},
  avatar:       {width:44,height:44,borderRadius:22,backgroundColor:C.fill,alignItems:'center',justifyContent:'center',borderWidth:1,borderColor:C.border},
  avatarTxt:    {color:C.green,fontSize:18,fontWeight:'800'},
  sheetHandle:  {alignSelf:'center',width:40,height:4,borderRadius:2,backgroundColor:C.fill,marginBottom:16},
});
