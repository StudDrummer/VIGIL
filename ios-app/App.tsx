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
import {
  ActivityIndicator,
  Alert,
  Animated,
  AppState,
  AppStateStatus,
  Dimensions,
  Image,
  KeyboardAvoidingView,
  Modal,
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
import Svg, {Circle} from 'react-native-svg';

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

// ─────────────────────────────────────────────
// CONFIG
// ─────────────────────────────────────────────
const DEFAULT_IP    = '100.107.17.113';
const DEFAULT_PORT  = '5001';
const TIMEOUT_MS    = 10_000;
const AUTO_SYNC_SEC = 60;
const VIGIL_KEY     = 'set-your-api-key-here';
const FALLBACK_URL  = `http://${DEFAULT_IP}:${DEFAULT_PORT}`;

const SK_ONBOARDING = '@vigil_onboarding_v1';
const SK_PROFILE    = '@vigil_profile_v1';
const SK_SERVER     = '@vigil_server_v1';

const AX = axios.create({
  timeout: TIMEOUT_MS,
  headers: {'X-VIGIL-Key': VIGIL_KEY, 'Content-Type': 'application/json'},
});

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
  bg:'#000000', card:'#16161A', cardHi:'#242428', fill:'#232329',
  border:'#2E2E34', hair:'rgba(255,255,255,0.06)',
  text:'#F5F5F7', sub:'#9B9BA3', muted:'#5A5A62',
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

function OnboardingScreen({onComplete,onDemo}:OBProps) {
  const [step,setStep]             = useState(0);
  const [residentName,setRN]       = useState('');
  const [dob,setDOB]               = useState('');
  const [residentHeight,setRH]     = useState('170');
  const [emergencyPhone,setEP]     = useState('');
  const [emergencyEmail,setEE]     = useState('');
  const [watchEnabled,setWE]       = useState(false);
  const [serverIp,setSI]           = useState(DEFAULT_IP);
  const [serverPort,setSP]         = useState(DEFAULT_PORT);
  const [testing,setTesting]       = useState(false);
  const [serverOk,setServerOk]     = useState<boolean|null>(null);
  const [agreed,setAgreed]         = useState(false);
  const TOTAL = 6;

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
    if(step===1)return residentName.trim().length>0 && residentHeight.trim().length>0;
    if(step===2)return emergencyPhone.trim().length>=10;
    return true;
  };

  const OBField = ({label,value,onChange,placeholder,keyboard='default',note}:any) => (
    <View style={{marginBottom:20}}>
      <Text style={{color:C.sub,fontSize:12,fontWeight:'600',letterSpacing:0.5,textTransform:'uppercase',marginBottom:8}}>{label}</Text>
      {note && <Text style={{color:C.muted,fontSize:12,marginBottom:8,lineHeight:17}}>{note}</Text>}
      <View style={{backgroundColor:C.card,borderRadius:12,borderWidth:1,borderColor:C.border}}>
        <TextInput
          style={{color:C.text,fontSize:15,paddingHorizontal:16,paddingVertical:14}}
          value={value} onChangeText={onChange} placeholder={placeholder}
          placeholderTextColor={C.muted} keyboardType={keyboard}
          autoCapitalize="none" autoCorrect={false}/>
      </View>
    </View>
  );

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
              ['VIGIL server','A Jetson Orin Nano running the VIGIL server software.'],
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
          <Text style={{color:C.text,fontSize:24,fontWeight:'800',marginBottom:6}}>Resident Profile</Text>
          <Text style={{color:C.sub,fontSize:14,marginBottom:28,lineHeight:20}}>
            This information personalises monitoring and is used to calibrate gait detection for this specific resident.
          </Text>
          <OBField label="Full Name" value={residentName} onChange={setRN} placeholder="e.g. Margaret Johnson"
            note="Used in alert messages sent to emergency contacts."/>
          <OBField label="Date of Birth" value={dob} onChange={setDOB} placeholder="MM/DD/YYYY"/>
          <OBField label="Height (cm)" value={residentHeight} onChange={setRH} placeholder="170" keyboard="number-pad"
            note="Used to calibrate walking speed measurements from the camera."/>
        </View>
      );
      case 2: return (
        <View style={{flex:1}}>
          <Text style={{color:C.text,fontSize:24,fontWeight:'800',marginBottom:6}}>Emergency Contact</Text>
          <Text style={{color:C.sub,fontSize:14,marginBottom:28,lineHeight:20}}>
            This person receives an SMS immediately when a fall or extended period of inactivity is detected.
          </Text>
          <View style={{backgroundColor:C.orange+'18',borderRadius:12,borderWidth:1,borderColor:C.orange+'44',padding:14,marginBottom:24}}>
            <Text style={{color:C.orange,fontSize:13,fontWeight:'600',marginBottom:4}}>Important</Text>
            <Text style={{color:C.orange,fontSize:12,lineHeight:18}}>
              The emergency contact receives real SMS messages on your Twilio number. Make sure this number is verified in your Twilio console during the trial period.
            </Text>
          </View>
          <OBField label="Phone Number" value={emergencyPhone} onChange={setEP} placeholder="+1 (206) 555-0000" keyboard="phone-pad"
            note="Must include country code. e.g. +12065550000"/>
          <OBField label="Email (optional)" value={emergencyEmail} onChange={setEE} placeholder="family@example.com" keyboard="email-address"/>
        </View>
      );
      case 3: return (
        <View style={{flex:1}}>
          <Text style={{color:C.text,fontSize:24,fontWeight:'800',marginBottom:6}}>Server Connection</Text>
          <Text style={{color:C.sub,fontSize:14,marginBottom:28,lineHeight:20}}>
            Enter the IP address of your VIGIL server (Jetson Orin Nano). Make sure your phone and the Jetson are on the same Tailscale network.
          </Text>
          <OBField label="Server IP Address" value={serverIp} onChange={setSI} placeholder="100.x.x.x"
            keyboard="numbers-and-punctuation"
            note="Find this by running: tailscale ip -4 on the Jetson."/>
          <OBField label="Port" value={serverPort} onChange={setSP} placeholder="5001" keyboard="number-pad"/>
          <TouchableOpacity
            style={{backgroundColor:testing?C.fill:C.blue,borderRadius:12,padding:14,alignItems:'center',marginBottom:16,opacity:testing?0.7:1}}
            onPress={testConnection} disabled={testing}>
            {testing
              ? <View style={{flexDirection:'row',gap:8,alignItems:'center'}}><ActivityIndicator color="#fff" size="small"/><Text style={{color:'#fff',fontWeight:'700'}}>Testing...</Text></View>
              : <Text style={{color:'#fff',fontSize:15,fontWeight:'700'}}>Test Connection</Text>}
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
                Make sure the Jetson is powered on, the VIGIL server is running (sudo systemctl status vigil), and Tailscale is connected on both devices.
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
          <Text style={{color:C.text,fontSize:24,fontWeight:'800',marginBottom:6}}>Apple Watch</Text>
          <Text style={{color:C.sub,fontSize:14,marginBottom:28,lineHeight:20}}>
            VIGIL works fully with the camera alone. Connecting an Apple Watch adds heart rate, HRV, sleep, and SpO2 data as additional health signals.
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
            ['Resident',residentName||'—'],
            ['Height',`${residentHeight} cm`],
            ['Emergency Contact',emergencyPhone||'—'],
            ['Server',`${serverIp}:${serverPort}`],
            ['Apple Watch',watchEnabled?'Connected':'Camera only'],
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

  const stepTitles = ['Welcome','Resident','Emergency','Server','Watch','Ready'];

  const finish = async () => {
    const profile:SavedProfile = {residentName,residentHeight,emergencyPhone,emergencyEmail,watchEnabled};
    await AsyncStorage.setItem(SK_PROFILE,JSON.stringify(profile));
    await AsyncStorage.setItem(SK_SERVER,JSON.stringify({ip:serverIp,port:serverPort}));
    await AsyncStorage.setItem(SK_ONBOARDING,'done');
    onComplete(profile,serverIp,serverPort);
  };

  return (
    <SafeAreaView style={{flex:1,backgroundColor:C.bg}}>
      <StatusBar barStyle="light-content"/>
      {/* Progress */}
      <View style={{paddingHorizontal:20,paddingTop:12,paddingBottom:4}}>
        <View style={{flexDirection:'row',gap:4,marginBottom:6}}>
          {Array.from({length:TOTAL}).map((_,i)=>(
            <View key={i} style={{flex:1,height:3,borderRadius:2,backgroundColor:i<=step?C.green:C.fill}}/>
          ))}
        </View>
        <Text style={{color:C.muted,fontSize:11,textAlign:'right'}}>{stepTitles[step]} · Step {step+1} of {TOTAL}</Text>
      </View>

      <KeyboardAvoidingView style={{flex:1}} behavior={Platform.OS==='ios'?'padding':undefined}>
        <ScrollView contentContainerStyle={{padding:24,paddingBottom:12}} keyboardShouldPersistTaps="handled" showsVerticalScrollIndicator={false}>
          {renderStep()}
        </ScrollView>
        <View style={{flexDirection:'row',gap:12,padding:20,paddingBottom:32}}>
          {step>0 && (
            <TouchableOpacity style={{flex:1,backgroundColor:C.fill,borderRadius:14,padding:16,alignItems:'center'}} onPress={()=>setStep(s=>s-1)}>
              <Text style={{color:C.text,fontWeight:'700',fontSize:15}}>Back</Text>
            </TouchableOpacity>
          )}
          <TouchableOpacity
            style={[s.btnPrimary,{flex:2},!canNext()&&{backgroundColor:C.fill,shadowOpacity:0}]}
            onPress={()=>step<TOTAL-1?setStep(s=>s+1):finish()}
            disabled={!canNext()}>
            <Text style={{color:canNext()?'#000':C.muted,fontWeight:'700',fontSize:15}}>
              {step===TOTAL-1?'Start Monitoring':'Continue'}
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
          <Text style={{color:C.muted,fontSize:11,marginTop:1}}>of time active today</Text>
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

// ─────────────────────────────────────────────
// AI INSIGHTS SCREEN
// ─────────────────────────────────────────────
function PersonDetailScreen({
  personId, personName, serverUrl, onClose, demoMode,
}:{personId:string; personName:string; serverUrl:string; onClose:()=>void;
   demoMode?:boolean}) {
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
        AX.get(`${u}/persons/${personId}/detections?days=14`,{timeout:8000}),
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
            <Text style={{color:C.sub,fontSize:10,fontWeight:'600',letterSpacing:0.5,textTransform:'uppercase',marginBottom:2}}>{label}</Text>
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
          <ScrollView showsVerticalScrollIndicator={false}>
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
function PersonPhotoCard({personId, serverUrl}:{personId:string; serverUrl:string}) {
  const [photo, setPhoto] = useState<string|null>(null);
  useEffect(()=>{
    AX.get(`${serverUrl||FALLBACK_URL}/persons/${personId}/photo`,{timeout:5000})
      .then(r=>{if(r.data?.available&&r.data?.photo_b64)setPhoto(r.data.photo_b64);})
      .catch(()=>{});
  },[personId,serverUrl]);
  if(!photo) return (
    <View style={{width:64,height:64,borderRadius:12,backgroundColor:C.fill,alignItems:'center',justifyContent:'center',marginBottom:10}}>
      <Text style={{color:C.muted,fontSize:24}}>?</Text>
    </View>
  );
  return (
    <Image source={{uri:`data:image/jpeg;base64,${photo}`}}
      style={{width:96,height:72,borderRadius:10,marginBottom:10}}
      resizeMode="cover"/>
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

function AIInsightsScreen({serverUrl,demoMode}:{serverUrl:string;demoMode?:boolean}) {
  const [residents,setResidents] = useState<ResidentInsights[]>([]);
  const [disclaimer,setDisclaimer] = useState<string>('');
  const [genAt,setGenAt]     = useState<string|null>(null);
  const [loading,setLoading] = useState(true);
  const [refreshing,setRefreshing] = useState(false);
  const [error,setError]     = useState<string|null>(null);
  const [face,setFace]       = useState<any>(null);
  const [sig,setSig]         = useState<any>(null);
  const [rout,setRout]       = useState<any>(null);
  const [pat,setPat]         = useState<any>(null);

  const load = useCallback(async(quiet?:boolean)=>{
    if(demoMode){
      setResidents(DEMO_INSIGHTS.residents as any);
      setDisclaimer(DEMO_INSIGHTS.disclaimer);
      setGenAt(DEMO_INSIGHTS.generated_at);
      setSig(DEMO_SIGNALS); setFace(DEMO_FACE); setRout(DEMO_ROUTINE); setPat(DEMO_PATTERNS);
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

      {residents.length===0 && (
        <View style={{alignItems:'center',padding:32}}>
          <Text style={{color:C.text,fontSize:16,fontWeight:'700'}}>No residents yet</Text>
          <Text style={{color:C.sub,fontSize:13,marginTop:6,textAlign:'center'}}>
            Identify a resident from the camera view to start building insights.
          </Text>
        </View>
      )}

      {residents.map(res=>{
        const attention = res.insights.filter(i=>i.severity==='attention').length;
        const watch     = res.insights.filter(i=>i.severity==='watch').length;
        const normal    = res.insights.filter(i=>i.severity==='normal').length;
        const pct = Math.max(0, Math.min(100, (res.days_collected/Math.max(1,res.days_needed))*100));

        return (
          <View key={res.person_id} style={{marginBottom:18}}>
            <Text style={{color:C.text,fontSize:18,fontWeight:'700',marginHorizontal:20,marginBottom:8}}>
              {res.name}
            </Text>

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
                  {res.recent_samples} readings in the last {res.recent_days} days
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
                        {it.current}<Text style={{color:C.sub,fontSize:15,fontWeight:'600'}}> {it.unit}</Text>
                      </Text>

                      <Text style={{color:C.sub,fontSize:13,marginTop:4}}>
                        {arrow} {Math.abs(it.delta_pct)}% vs baseline {it.baseline_mean} {it.unit}
                      </Text>
                      {it.z_score!==null && (
                        <Text style={{color:C.muted,fontSize:11,marginTop:3}}>
                          {Math.abs(it.z_score)} SD {it.direction==='up'?'above':'below'} normal
                          {it.baseline_days?` \u00b7 ${it.baseline_days}-day baseline`:''}
                        </Text>
                      )}

                      <View style={{height:1,backgroundColor:C.border,marginVertical:12}}/>
                      <Text style={{color:C.sub,fontSize:12,lineHeight:18}}>{it.note}</Text>
                    </View>
                  );
                })}
              </>
            )}
          </View>
        );
      })}

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
                    {s.label}
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
                  {arrow} {Math.abs(s.delta_pct)}% vs {s.baseline_mean} {s.unit} norm
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

export default function App() {

  // ── Boot state ──
  const [booting,setBooting]           = useState(true);
  const [showOnboarding,setOnboarding] = useState(false);
  const [demoMode,setDemoMode]         = useState(false);

  // ── Connection ──
  const [initialized,setInitialized]   = useState(false);
  const [initError,setInitError]       = useState<string|null>(null);
  const [jetsonOk,setJetsonOk]         = useState<boolean|null>(null);
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
  const [isResidentFlag,setIsResidentFlag]   = useState(false);
  const [selectedPerson,setSelectedPerson]   = useState<Person|null>(null);
  const [fullscreen,setFullscreen]     = useState(false);
  const [expandedAlert,setExpandedAlert]= useState<number|null>(null);

  // ── Settings ──
  const [residentName,setResidentName]   = useState('');
  const [residentHeight,setResidentHeight]= useState('170');
  const [emergencyPhone,setEmergencyPhone]= useState('');
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
        const [done,profileStr,serverStr]=await Promise.all([
          AsyncStorage.getItem(SK_ONBOARDING),
          AsyncStorage.getItem(SK_PROFILE),
          AsyncStorage.getItem(SK_SERVER),
        ]);
        if(profileStr){
          const p:SavedProfile=JSON.parse(profileStr);
          setResidentName(p.residentName||'');
          setResidentHeight(p.residentHeight||'170');
          setEmergencyPhone(p.emergencyPhone||'');
          setEmergencyEmail(p.emergencyEmail||'');
          setWatchEnabled(p.watchEnabled||false);
        }
        if(serverStr){
          const sv=JSON.parse(serverStr);
          const ip=sv.ip||DEFAULT_IP; const port=sv.port||DEFAULT_PORT;
          setServerIp(ip);setServerPort(port);
          const u=`http://${ip}:${port}`;urlRef.current=u;
        }
        if(done!=='done'){setOnboarding(true);}
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
      const r=await AX.get(`${url()}/camera/snapshot`,{timeout:4000});
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

  const fetchPersonAlerts=useCallback(async()=>{
    try{
      const r=await AX.get(`${url()}/persons/alerts`,{timeout:5000});
      if(Array.isArray(r.data)&&r.data.length>0){
        setPersonAlerts(r.data);
        setPendingPersonAlert(prev=>{
          if(!showPersonModal && !prev){
            setShowPersonModal(true);
            return r.data[0];
          }
          return prev;
        });
      }
    }catch(_){}
  },[showPersonModal]);

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
    try{
      await AX.post(`${url()}/alerts/ack`,{id, acknowledged_by:'Caregiver'});
      setAlerts(p=>p.map(a=>a.id===id?{...a,seen:1}:a));
    }catch(e:any){
      log('ERROR',`Could not acknowledge alert ${id}: ${e?.message??String(e)}`);
    }
  },[log]);

  const loadConfig=useCallback(async()=>{
    try{
      const r=await AX.get(`${url()}/config`);
      if(r.data){
        if(r.data.resident_name)setResidentName(r.data.resident_name);
        if(r.data.emergency_phone)setEmergencyPhone(r.data.emergency_phone);
        if(r.data.emergency_email)setEmergencyEmail(r.data.emergency_email);
        if(r.data.resident_height_cm)setResidentHeight(String(r.data.resident_height_cm));
      }
    }catch(_){}
  },[]);

  // ─────────────────────────────────────────────
  // HEALTHKIT INIT
  // ─────────────────────────────────────────────
  useEffect(()=>{
    if(!AppleHealthKit){log('WARN',`HealthKit not available — ${HK_LOAD_ERROR??'unknown load failure'}`);return;}
    const HKP=AppleHealthKit?.Constants?.Permissions;
    if(!HKP||typeof AppleHealthKit.initHealthKit!=='function'){
      log('ERROR',`HK JS wrapper loaded but native module not linked (Constants=${!!HKP}, init=${typeof AppleHealthKit.initHealthKit}). Rebuild pods: cd ios && pod install, then Clean Build Folder.`);
      return;
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
    AppleHealthKit.initHealthKit(perms,(err:string)=>{
      if(err){log('ERROR',`HK: ${err}`);setInitError(err);return;}
      log('OK','HealthKit ready');setInitialized(true);
    });
  },[log]);

  // ─────────────────────────────────────────────
  // SERVER POLLING EFFECTS
  // ─────────────────────────────────────────────
  useEffect(()=>{if(!booting&&!showOnboarding&&!demoMode)setTimeout(()=>pingJetson(),1500);},[booting,showOnboarding,demoMode,pingJetson]);

  useEffect(()=>{
    if(!initialized||jetsonOk!==true||demoMode)return;
    fetchDashboard();fetchAlerts();loadConfig();fetchRooms();
    setCountdown(AUTO_SYNC_SEC);
    cntTimer.current=setInterval(()=>setCountdown(p=>p<=1?AUTO_SYNC_SEC:p-1),1000);
    autoTimer.current=setInterval(()=>performSync(true),AUTO_SYNC_SEC*1000);
    performSync(true);
    return()=>{if(autoTimer.current)clearInterval(autoTimer.current);if(cntTimer.current)clearInterval(cntTimer.current);};
  },[initialized,jetsonOk]); // eslint-disable-line

  useEffect(()=>{if(!jetsonOk||demoMode||showPersonModal)return;fetchCameraStatus();const id=setInterval(fetchCameraStatus,10000);return()=>clearInterval(id);},[jetsonOk,showPersonModal,fetchCameraStatus]);
  useEffect(()=>{if(!jetsonOk||demoMode||showPersonModal)return;fetchCameraSnapshot();const id=setInterval(fetchCameraSnapshot,2500);return()=>clearInterval(id);},[jetsonOk,showPersonModal,fetchCameraSnapshot]);
  // Camera-derived metrics (gait speed / cadence / posture) refresh every 4s so they
  // visibly move while someone is walking in frame — the 60s HealthKit auto-sync
  // (above) is far too slow for this to feel live during a walk-test or demo.
  useEffect(()=>{if(!jetsonOk||demoMode||showPersonModal)return;fetchDashboard();const id=setInterval(fetchDashboard,4000);return()=>clearInterval(id);},[jetsonOk,showPersonModal,fetchDashboard]);
  useEffect(()=>{if(!jetsonOk||demoMode||showPersonModal)return;const id=setInterval(fetchRooms,15000);return()=>clearInterval(id);},[jetsonOk,showPersonModal,fetchRooms]);
  useEffect(()=>{if(!jetsonOk||demoMode||showPersonModal)return;fetchPersons();const id=setInterval(fetchPersons,20000);return()=>clearInterval(id);},[jetsonOk,showPersonModal,fetchPersons]);
  // Tightened from 30s -> 6s: this is the "more than one person detected" identification
  // sheet — for a live demo this needs to feel near-instant when a second person walks in.
  useEffect(()=>{if(!jetsonOk||demoMode||showPersonModal)return;fetchPersonAlerts();const id=setInterval(fetchPersonAlerts,6000);return()=>clearInterval(id);},[jetsonOk,showPersonModal,fetchPersonAlerts]);
  // Tightened from 15s -> 5s: fall/stillness alerts should reach the UI almost as fast
  // as the SMS itself arrives, not lag up to 15s behind it.
  useEffect(()=>{if(!jetsonOk||demoMode||showPersonModal)return;const id=setInterval(fetchAlerts,5000);return()=>clearInterval(id);},[jetsonOk,showPersonModal,fetchAlerts]);

  // ─────────────────────────────────────────────
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
  const saveProfile=useCallback(async()=>{
    setSavingProfile(true);
    try{
      await AX.post(`${url()}/config`,{
        resident_name:residentName||null,
        emergency_phone:emergencyPhone||null,
        emergency_email:emergencyEmail||null,
        resident_height_cm:parseFloat(residentHeight)||170,
      });
      const profile:SavedProfile={residentName,residentHeight,emergencyPhone,emergencyEmail,watchEnabled};
      await AsyncStorage.setItem(SK_PROFILE,JSON.stringify(profile));
      Alert.alert('Saved','Profile saved and synced to your VIGIL server.');
    }catch(e:any){Alert.alert('Save Failed',`Could not reach server: ${e?.message}`);}
    finally{setSavingProfile(false);}
  },[residentName,emergencyPhone,emergencyEmail,residentHeight,watchEnabled]);

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

  const CameraFeed=({fullSize=false}:{fullSize?:boolean})=>{
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
  const renderToday=()=>(
    <ScrollView refreshControl={<RefreshControl refreshing={refreshing} onRefresh={onRefresh} tintColor={C.green}/>} showsVerticalScrollIndicator={false}>
      <View style={s.pageHead}>
        <Text style={s.pageTitle}>Today</Text>
        <Text style={s.pageSub}>{today?.date??new Date().toISOString().slice(0,10)}</Text>
      </View>

      {/* Status pills */}
      <View style={s.pillRow}>
        <View style={s.pill}><View style={[s.pillDot,{backgroundColor:initialized?C.green:initError?C.red:C.sub}]}/><Text style={s.pillTxt}>{initialized?'HealthKit':initError?'HK Error':'Starting'}</Text></View>
        <View style={s.pill}><View style={[s.pillDot,{backgroundColor:jetsonOk===true?C.green:jetsonOk===false?C.red:C.sub}]}/><Text style={s.pillTxt}>{jetsonOk===true?'Server':jetsonOk===false?'Offline':'Checking'}</Text></View>
        {jetsonOk===true&&<View style={s.pill}><View style={[s.pillDot,{backgroundColor:syncing?C.orange:C.green}]}/><Text style={s.pillTxt}>{syncing?'Syncing':`${countdown}s`}</Text></View>}
        {cameraStatus?.online&&<View style={s.pill}><View style={[s.pillDot,{backgroundColor:C.green}]}/><Text style={s.pillTxt}>{String(cameraStatus.fps?.toFixed(0)??'?')} fps</Text></View>}
      </View>

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
              {cameraStatus?.online&&cameraStatus?.person_present?'Resident Detected':cameraStatus?.online?'Room Empty':'Camera Offline'}
            </Text>
            <Text style={{color:C.sub,fontSize:12,marginTop:2}}>
              {cameraStatus?.online?`${cameraStatus.fps?.toFixed(1)??'?'} fps · Live`:'Check Jetson connection'}
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
      <View style={{marginHorizontal:16,marginBottom:4}}><CameraFeed/></View>

      {/* Activity ring */}
      <Text style={s.secHeader}>Activity</Text>
      <ActivityRing pct={today?.activity_level_pct??null} activeMin={today?.active_minutes??null} stillMin={today?.still_minutes??null}/>

      {/* Camera metrics */}
      <Text style={s.secHeader}>Camera Metrics</Text>
      <View style={s.grid}>
        {[
          {label:'Gait Speed',val:fmt(today?.camera_gait_speed_ms,2),unit:'m/s',color:C.green,warn:(today?.camera_gait_speed_ms??1)<0.8&&today?.camera_gait_speed_ms!=null},
          {label:'Posture',val:fmt(today?.posture_score,0),unit:'/100',color:C.purple,warn:(today?.posture_score??100)<50&&today?.posture_score!=null},
          {label:'Asymmetry',val:fmt(today?.camera_asymmetry_pct,1),unit:'%',color:C.orange,warn:(today?.camera_asymmetry_pct??0)>15},
          {label:'Cadence',val:fmt(today?.camera_cadence_spm,0),unit:'spm',color:C.blue,warn:false},
          {label:'Longest Still',val:fmt(today?.longest_stillness_min,0),unit:'min',color:C.orange,warn:(today?.longest_stillness_min??0)>90},
          {label:'Data Points',val:fmtLarge(today?.camera_data_points),unit:'pts',color:C.sub,warn:false},
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
      {watchEnabled&&(today?.hrv_sdnn!=null||today?.resting_hr!=null)&&(
        <>
          <Text style={s.secHeader}>Apple Watch</Text>
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
              <Text style={{color:C.sub,fontSize:10,fontWeight:'600',letterSpacing:0.5,textTransform:'uppercase',marginBottom:2}}>{label}</Text>
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

    return(
      <ScrollView showsVerticalScrollIndicator={false}>
        <View style={s.pageHead}>
          <Text style={s.pageTitle}>Trends</Text>
          <Text style={s.pageSub}>{history.length} days recorded</Text>
        </View>
        {history.length===0?(
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
            {history.some(r=>r.hrv_sdnn!=null)&&(
              <>
                <Text style={s.secHeader}>Apple Watch — This Week</Text>
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
    if(a.alert_type==='fall')return`A fall was detected. Hip drop: ${Math.round((p.hip_drop_pct||0)*100)}%, torso angle: ${Math.round(p.torso_angle||0)}°. Emergency contact notified by SMS.`;
    if(a.alert_type==='stillness')return`No movement detected for over ${p.duration_min||'?'} minutes. This is outside the normal pattern for this resident.`;
    if(a.alert_type==='health_signal'&&p.metric&&p.value!=null&&p.baseline!=null){
      const pct=p.baseline!==0?Math.round(Math.abs((p.value-p.baseline)/p.baseline)*100):0;
      const dir=p.value>p.baseline?'above':'below';
      return`${p.metric} today (${Number(p.value).toFixed(1)}) is ${pct}% ${dir} this resident's 14-day baseline (${Number(p.baseline).toFixed(1)}).`;
    }
    return p.message??'An alert was triggered by VIGIL.';
  };

  const renderAlerts=()=>(
    <ScrollView showsVerticalScrollIndicator={false}>
      <View style={s.pageHead}>
        <Text style={s.pageTitle}>Alerts</Text>
        <Text style={s.pageSub}>{alertCount>0?`${alertCount} unacknowledged`:'All clear'}</Text>
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
          : a.alert_type==='multiple_persons' ? C.purple
          : a.alert_type==='absence_pattern'  ? C.teal
          : C.blue;
        const badge = a.alert_type==='fall' ? 'FALL DETECTED'
          : a.alert_type==='stillness'        ? 'STILLNESS ALERT'
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
            <View style={{marginTop:10,padding:12,backgroundColor:color+'12',borderRadius:10,borderLeftWidth:2,borderLeftColor:color}}>
              <Text style={{color:C.text,fontSize:13,lineHeight:20}}>{explainAlert(a)}</Text>
            </View>
            <View style={{flexDirection:'row',gap:12,marginTop:8}}>
              <TouchableOpacity onPress={()=>setExpandedAlert(isExp?null:a.id)}>
                <Text style={{color:C.muted,fontSize:11}}>{isExp?'Hide details':'Show details'}</Text>
              </TouchableOpacity>
              {!a.seen&&(
                <TouchableOpacity onPress={()=>acknowledgeAlert(a.id)}>
                  <Text style={{color:C.green,fontSize:11,fontWeight:'600'}}>Acknowledge</Text>
                </TouchableOpacity>
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
  const OField=({label,value,onChange,placeholder,keyboard='default',note}:any)=>(
    <View style={{paddingHorizontal:16,paddingVertical:12,borderBottomWidth:1,borderBottomColor:C.hair}}>
      <View style={{flexDirection:'row',alignItems:'center',justifyContent:'space-between'}}>
        <Text style={{color:C.text,fontSize:15}}>{label}</Text>
        <TextInput style={{color:C.text,fontSize:15,textAlign:'right',flex:1,marginLeft:16,paddingVertical:0}} value={value} onChangeText={onChange} placeholder={placeholder} placeholderTextColor={C.muted} keyboardType={keyboard} autoCapitalize="none" autoCorrect={false}/>
      </View>
      {note&&<Text style={{color:C.muted,fontSize:11,marginTop:4}}>{note}</Text>}
    </View>
  );

  const renderResidentTab=()=>(
    <ScrollView keyboardShouldPersistTaps="handled">
      <View style={[s.settGrp,{marginTop:16}]}>
        <OField label="Resident Name" value={residentName} onChange={setResidentName} placeholder="Not set"/>
        <OField label="Height (cm)" value={residentHeight} onChange={setResidentHeight} placeholder="170" keyboard="number-pad" note="Used to calibrate gait speed measurements."/>
        <OField label="Emergency Phone" value={emergencyPhone} onChange={setEmergencyPhone} placeholder="+1 (206) 555-0000" keyboard="phone-pad" note="Receives SMS on fall or extended inactivity."/>
        <OField label="Email" value={emergencyEmail} onChange={setEmergencyEmail} placeholder="Optional" keyboard="email-address"/>
        <View style={{flexDirection:'row',alignItems:'center',justifyContent:'space-between',paddingHorizontal:16,paddingVertical:14}}>
          <View>
            <Text style={{color:C.text,fontSize:15}}>Apple Watch</Text>
            <Text style={{color:C.muted,fontSize:11,marginTop:2}}>Show Apple Watch metrics on dashboard</Text>
          </View>
          <Switch value={watchEnabled} onValueChange={setWatchEnabled} trackColor={{false:C.fill,true:C.green}} thumbColor="#FFFFFF"/>
        </View>
      </View>
      <TouchableOpacity style={[s.btnPrimary,{marginHorizontal:16,marginTop:20,opacity:savingProfile?0.6:1}]} onPress={saveProfile} disabled={savingProfile}>
        {savingProfile?<ActivityIndicator color="#000" size="small"/>:<Text style={s.btnPrimaryTxt}>Save Profile</Text>}
      </TouchableOpacity>
      <Text style={{color:C.muted,fontSize:12,textAlign:'center',marginTop:10,marginHorizontal:16}}>
        Profile is synced to your VIGIL server and used for emergency alert messages.
      </Text>
      <View style={{height:32}}/>
    </ScrollView>
  );

  const renderServerTab=()=>(
    <ScrollView>
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
      <View style={{height:32}}/>
    </ScrollView>
  );

  const renderCameraTab=()=>(
    <ScrollView showsVerticalScrollIndicator={false}>
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
      <View style={{marginHorizontal:16}}><CameraFeed/></View>
      <Text style={{color:C.muted,fontSize:11,marginLeft:16,marginTop:6,marginBottom:4}}>Updates every 2.5 seconds · Tap for full screen</Text>
      <Text style={s.secHeader}>Setup Guide</Text>
      {[
        {n:'1',title:'Place the Camera',desc:'Mount the camera 5–7 feet high, angled slightly downward. Position it to see the resident\'s full body when standing in their main living area.'},
        {n:'2',title:'Connect to Wi-Fi',desc:'Open the Tapo or Reolink app, add your camera, and connect it to the same Wi-Fi network as the VIGIL server.'},
        {n:'3',title:'Enable RTSP Stream',desc:'In the camera app, go to Advanced Settings and enable RTSP. Note the camera IP address.'},
        {n:'4',title:'Start Monitoring',desc:'On the Jetson server, the camera service starts automatically. VIGIL begins learning the resident\'s normal patterns immediately.'},
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
          {persons.map(p=>(
            <TouchableOpacity
              key={p.id}
              disabled={!p.name}
              activeOpacity={p.name?0.6:1}
              onPress={()=>{if(p.name)setSelectedPerson(p);}}
              style={[s.card,{marginHorizontal:16,marginBottom:8,flexDirection:'row',alignItems:'center',justifyContent:'space-between'}]}>
              <View style={{flexDirection:'row',alignItems:'center',gap:10,flex:1,minWidth:0}}>
                <View style={[s.avatar,{backgroundColor:p.is_resident?C.green+'22':C.blue+'22',flexShrink:0}]}>
                  <Text style={[s.avatarTxt,{color:p.is_resident?C.green:C.blue}]}>{(p.name??'?').charAt(0).toUpperCase()}</Text>
                </View>
                <View style={{flex:1,minWidth:0}}>
                  <Text style={{color:C.text,fontSize:14,fontWeight:'600'}} numberOfLines={1} ellipsizeMode="tail">{p.name??'Unidentified Person'}</Text>
                  <Text style={{color:C.sub,fontSize:11,marginTop:2}} numberOfLines={1} ellipsizeMode="tail">
                    {p.is_resident?'Resident':'Visitor'}{` · ${Math.round(p.confidence*100)}%`}{p.name?' · Tap for trends':''}
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
                <Text style={{color:C.muted,fontSize:16}}>{'>'}</Text>
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
    <ScrollView>
      <Text style={s.secHeader}>App Info</Text>
      <View style={s.settGrp}>
        <View style={{flexDirection:'row',justifyContent:'space-between',paddingHorizontal:16,paddingVertical:14}}><Text style={{color:C.text,fontSize:15}}>App Version</Text><Text style={{color:C.sub,fontSize:14,fontWeight:'500'}}>VIGIL v1.0.0</Text></View>
        {serverVersion&&<View style={{flexDirection:'row',justifyContent:'space-between',paddingHorizontal:16,paddingVertical:14,borderTopWidth:1,borderTopColor:C.hair}}><Text style={{color:C.text,fontSize:15}}>Server Version</Text><Text style={{color:C.sub,fontSize:14,fontWeight:'500'}}>{serverVersion}</Text></View>}
        <View style={{flexDirection:'row',justifyContent:'space-between',paddingHorizontal:16,paddingVertical:14,borderTopWidth:1,borderTopColor:C.hair}}><Text style={{color:C.text,fontSize:15}}>HealthKit</Text><Text style={{color:initialized?C.green:initError?C.red:C.sub,fontSize:14,fontWeight:'500'}}>{initialized?'Connected':initError?'Error':'Starting'}</Text></View>
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
            <Text style={{color:settingsTab===t?C.green:C.sub,fontSize:13,fontWeight:'600'}}>{t.charAt(0).toUpperCase()+t.slice(1)}</Text>
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
      <OnboardingScreen onDemo={()=>{setOnboarding(false);enterDemo();}} onComplete={(profile,ip,port)=>{
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
            {cameraStatus?.person_present?'Resident Detected':'Room Empty'} · {cameraStatus?.fps?.toFixed(1)??'?'} fps
          </Text>
        </View>
      </Modal>

      {/* Person identification modal */}
      <Modal visible={showPersonModal && !!pendingPersonAlert} animationType="slide" transparent statusBarTranslucent>
        <View style={{flex:1,backgroundColor:'rgba(0,0,0,0.85)',justifyContent:'flex-end'}}>
          <View style={{backgroundColor:C.card,borderTopLeftRadius:24,borderTopRightRadius:24,padding:24,paddingBottom:48}}>
            <View style={s.sheetHandle}/>
            <Text style={{color:C.text,fontSize:22,fontWeight:'800',marginBottom:8}}>Identify People in Frame</Text>
            <Text style={{color:C.sub,fontSize:14,lineHeight:21,marginBottom:24}}>
              {`VIGIL detected ${pendingPersonAlert?.person_ids?.length??1} ${(pendingPersonAlert?.person_ids?.length??1)===1?'person':'people'} on camera. Identify them so VIGIL can track each person separately.`}
            </Text>
            <ScrollView style={{maxHeight:320}} showsVerticalScrollIndicator={false}>
            {(pendingPersonAlert?.person_ids??[]).map((pid,idx)=>{
              const existing=persons.find(p=>p.id===pid);
              const isNaming=namingPersonId===pid;
              return(
                <View key={pid} style={{backgroundColor:C.fill,borderRadius:16,padding:16,marginBottom:12}}>
                  <PersonPhotoCard personId={pid} serverUrl={urlRef.current}/>
                  <Text style={{color:C.sub,fontSize:11,fontWeight:'600',letterSpacing:0.5,textTransform:'uppercase',marginBottom:8}}>{`Person ${idx+1}`}</Text>
                  {existing?.name?(
                    <Text style={{color:C.green,fontSize:15,fontWeight:'600'}}>{existing.name}{existing.is_resident?' (Resident)':''}</Text>
                  ):isNaming?(
                    <KeyboardAvoidingView behavior={Platform.OS==='ios'?'padding':undefined}>
                      <View style={{gap:10}}>
                        <TextInput
                          style={{backgroundColor:C.card,color:C.text,fontSize:16,padding:12,borderRadius:10,borderWidth:1,borderColor:C.border}}
                          placeholder="Enter name" placeholderTextColor={C.muted}
                          value={newPersonName} onChangeText={setNewPersonName} autoFocus/>
                        <View style={{flexDirection:'row',gap:8,alignItems:'center'}}>
                          <Switch value={isResidentFlag} onValueChange={setIsResidentFlag} trackColor={{false:C.fill,true:C.green}} thumbColor="#FFF"/>
                          <Text style={{color:C.sub,fontSize:13}}>This is the monitored resident</Text>
                        </View>
                        <TouchableOpacity
                          style={{backgroundColor:C.green,borderRadius:12,padding:12,alignItems:'center',opacity:newPersonName.length>0?1:0.5}}
                          disabled={newPersonName.length===0}
                          onPress={()=>identifyPerson(pid,newPersonName,isResidentFlag,pendingPersonAlert?.id)}>
                          <Text style={{color:'#000',fontWeight:'700',fontSize:14}}>Confirm</Text>
                        </TouchableOpacity>
                      </View>
                    </KeyboardAvoidingView>
                  ):(
                    <View style={{flexDirection:'row',gap:8,flexWrap:'wrap'}}>
                      <TouchableOpacity
                        style={{backgroundColor:C.green,paddingHorizontal:14,paddingVertical:8,borderRadius:10}}
                        onPress={()=>{setNamingPersonId(pid);setIsResidentFlag(idx===0);}}>
                        <Text style={{color:'#000',fontWeight:'600',fontSize:13}}>Name this person</Text>
                      </TouchableOpacity>
                      <TouchableOpacity
                        style={{backgroundColor:C.fill,paddingHorizontal:14,paddingVertical:8,borderRadius:10,borderWidth:1,borderColor:C.border}}
                        onPress={()=>identifyPerson(pid,'Visitor',false,pendingPersonAlert?.id)}>
                        <Text style={{color:C.sub,fontWeight:'600',fontSize:13}}>Mark as Visitor</Text>
                      </TouchableOpacity>
                      {persons.filter(p=>p.name&&p.name!=='Visitor'&&p.id!==pid).length>0&&(
                        <View style={{width:'100%',marginTop:6}}>
                          <Text style={{color:C.muted,fontSize:11,marginBottom:8}}>
                            Or add to someone VIGIL already knows
                          </Text>
                          <View style={{flexDirection:'row',gap:8,flexWrap:'wrap'}}>
                            {persons.filter(p=>p.name&&p.name!=='Visitor'&&p.id!==pid).map(p=>(
                              <TouchableOpacity key={p.id}
                                onPress={()=>mergePerson(pid,p.id,pendingPersonAlert?.id)}
                                style={{backgroundColor:C.card,paddingHorizontal:12,paddingVertical:8,
                                        borderRadius:10,borderWidth:1,borderColor:C.border}}>
                                <Text style={{color:C.text,fontSize:13,fontWeight:'600'}}>{p.name}</Text>
                              </TouchableOpacity>
                            ))}
                          </View>
                        </View>
                      )}
                    </View>
                  )}
                </View>
              );
            })}
            </ScrollView>
            <TouchableOpacity style={{marginTop:8,padding:14,alignItems:'center'}} onPress={()=>{setShowPersonModal(false);setPendingPersonAlert(null);}}>
              <Text style={{color:C.sub,fontSize:14}}>Remind me later</Text>
            </TouchableOpacity>
          </View>
        </View>
      </Modal>

      {/* Person detail screen */}
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
        {activeTab==='insights'&&<AIInsightsScreen serverUrl={urlRef.current} demoMode={demoMode}/>}
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
                <Text style={{fontSize:20,color,lineHeight:24,textAlign:'center'}}>{icon}</Text>
                {key==='alerts'&&alertCount>0&&(
                  <View style={s.badge}><Text style={s.badgeTxt}>{alertCount>9?'9+':String(alertCount)}</Text></View>
                )}
              </View>
              <Text style={{fontSize:10.5,color,fontWeight:active?'700':'500',marginTop:2,letterSpacing:0.3}}>{label}</Text>
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
  pageTitle:    {color:C.text,fontSize:34,fontWeight:'800',letterSpacing:-0.6},
  pageSub:      {color:C.sub,fontSize:14,marginTop:3},
  secHeader:    {color:C.sub,fontSize:11,fontWeight:'700',letterSpacing:1.2,textTransform:'uppercase',marginLeft:20,marginTop:32,marginBottom:10},
  pillRow:      {flexDirection:'row',paddingHorizontal:16,gap:8,marginBottom:4,flexWrap:'wrap'},
  pill:         {flexDirection:'row',alignItems:'center',backgroundColor:C.card,borderRadius:100,paddingHorizontal:11,paddingVertical:6,gap:6,borderWidth:1,borderColor:C.hair},
  pillDot:      {width:7,height:7,borderRadius:4},
  pillTxt:      {color:C.sub,fontSize:12,fontWeight:'600'},
  card:         {backgroundColor:C.card,borderRadius:18,padding:16,borderWidth:1,borderColor:C.hair,shadowColor:'#000',shadowOpacity:0.12,shadowRadius:8,shadowOffset:{width:0,height:2}},
  grid:         {flexDirection:'row',flexWrap:'wrap',paddingHorizontal:12,gap:10},
  metCard:      {width:(SW-44)/2,backgroundColor:C.card,borderRadius:20,padding:14,borderWidth:1,borderColor:C.hair,borderTopWidth:1,borderTopColor:'rgba(255,255,255,0.07)',shadowColor:'#000',shadowOpacity:0.12,shadowRadius:8,shadowOffset:{width:0,height:2}},
  metTop:       {flexDirection:'row',alignItems:'center',gap:6,marginBottom:10},
  metDot:       {width:7,height:7,borderRadius:4},
  metLbl:       {color:C.sub,fontSize:10,fontWeight:'600',textTransform:'uppercase',letterSpacing:0.8},
  metVal:       {color:C.text,fontSize:28,fontWeight:'800',letterSpacing:-0.8,fontVariant:['tabular-nums']},
  metUnit:      {color:C.sub,fontSize:11,marginTop:3},
  warnChip:     {alignSelf:'flex-start',marginTop:8,backgroundColor:'rgba(255,69,58,0.14)',borderRadius:100,paddingHorizontal:8,paddingVertical:3},
  warnChipTxt:  {color:C.red,fontSize:9,fontWeight:'700',letterSpacing:0.4},
  empty:        {alignItems:'center',paddingVertical:56,paddingHorizontal:32},
  emptyGlyph:   {width:60,height:60,borderRadius:30,backgroundColor:C.fill,alignItems:'center',justifyContent:'center',marginBottom:14,borderWidth:1,borderColor:C.hair},
  emptyGlyphTxt:{color:C.sub,fontSize:22,fontWeight:'700'},
  emptyTxt:     {color:C.text,fontSize:16,fontWeight:'600'},
  emptySub:     {color:C.muted,fontSize:13,textAlign:'center',marginTop:5,lineHeight:19},
  settGrp:      {marginHorizontal:16,backgroundColor:C.card,borderRadius:18,overflow:'hidden',borderWidth:1,borderColor:C.hair},
  btnPrimary:   {backgroundColor:C.green,borderRadius:16,padding:16,alignItems:'center',shadowColor:C.green,shadowOpacity:0.35,shadowRadius:14,shadowOffset:{width:0,height:5}},
  btnPrimaryTxt:{color:'#000000',fontSize:15,fontWeight:'800',letterSpacing:0.2},
  btnSecondary: {backgroundColor:C.fill,borderRadius:16,padding:14,alignItems:'center',borderWidth:1,borderColor:C.hair},
  btnSecondaryTxt:{color:C.text,fontSize:14,fontWeight:'600'},
  tabBar:       {flexDirection:'row',backgroundColor:'#0A0A0C',borderTopWidth:1,borderTopColor:C.hair,paddingBottom:22,paddingTop:8},
  tabIndicator: {position:'absolute',top:-8,alignSelf:'center',width:24,height:3,borderRadius:2,backgroundColor:C.green},
  badge:        {position:'absolute',top:-3,right:-8,backgroundColor:C.red,borderRadius:7,minWidth:15,height:15,alignItems:'center',justifyContent:'center',paddingHorizontal:3,borderWidth:1.5,borderColor:'#0A0A0C'},
  badgeTxt:     {color:'#fff',fontSize:9,fontWeight:'800'},
  avatar:       {width:44,height:44,borderRadius:22,backgroundColor:C.fill,alignItems:'center',justifyContent:'center',borderWidth:1,borderColor:C.border},
  avatarTxt:    {color:C.green,fontSize:18,fontWeight:'800'},
  sheetHandle:  {alignSelf:'center',width:40,height:4,borderRadius:2,backgroundColor:C.fill,marginBottom:16},
});
