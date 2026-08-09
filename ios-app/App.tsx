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
const VIGIL_KEY     = 'vigil-api-key'; // Set via environment config
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
interface OBProps { onComplete:(profile:SavedProfile,serverIp:string,serverPort:string)=>void; }

function OnboardingScreen({onComplete}:OBProps) {
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
// ML INSIGHTS SCREEN
// ─────────────────────────────────────────────
function PersonDetailScreen({
  personId, personName, serverUrl, onClose,
}:{personId:string; personName:string; serverUrl:string; onClose:()=>void}) {
  const [trends,setTrends]         = useState<PersonTrendDay[]>([]);
  const [baseline,setBaseline]     = useState<PersonBaselineStatus|null>(null);
  const [weeklyTrend,setWeeklyTrend] = useState<any>(null);
  const [pdLoading,setPdLoading]   = useState(true);

  const load = useCallback(async()=>{
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
  },[serverUrl,personId]);

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

function MLInsightsScreen({serverUrl}:{serverUrl:string}) {
  const [scores,setScores]     = useState<MLScore[]>([]);
  const [scoredAt,setScoredAt] = useState<string|null>(null);
  const [loading,setLoading]   = useState(true);
  const [error,setError]       = useState<string|null>(null);

  const load = useCallback(async()=>{
    setLoading(true);setError(null);
    try{
      const r=await AX.get(`${serverUrl||FALLBACK_URL}/ml/scores/latest`,{timeout:8000});
      setScores(r.data?.scores??[]);setScoredAt(r.data?.scored_at??null);
    }catch(e:any){setError(e?.message??'Request failed');}
    finally{setLoading(false);}
  },[serverUrl]);

  useEffect(()=>{load();},[load]);

  const lc=(s:number)=>s>=70?C.red:s>=50?C.orange:C.green;
  const ll=(s:number)=>s>=70?'HIGH':s>=50?'ELEVATED':'LOW';
  const high=scores.filter(s=>s.score_0_100>=70&&!s.insufficient_data).length;
  const elev=scores.filter(s=>s.score_0_100>=50&&s.score_0_100<70&&!s.insufficient_data).length;
  const low=scores.filter(s=>s.score_0_100<50&&!s.insufficient_data).length;

  if(loading)return<View style={{flex:1,alignItems:'center',justifyContent:'center',backgroundColor:C.bg}}><ActivityIndicator color={C.green} size="large"/><Text style={{color:C.sub,marginTop:12}}>Loading ML scores...</Text></View>;
  if(error)return(
    <View style={{flex:1,alignItems:'center',justifyContent:'center',backgroundColor:C.bg,padding:32}}>
      <Text style={{color:C.text,fontSize:18,fontWeight:'700',marginBottom:8}}>Cannot reach server</Text>
      <Text style={{color:C.sub,fontSize:13,textAlign:'center',marginBottom:16}}>{error}</Text>
      <TouchableOpacity style={{backgroundColor:C.blue,paddingHorizontal:24,paddingVertical:12,borderRadius:12}} onPress={load}>
        <Text style={{color:'#fff',fontWeight:'700',fontSize:14}}>Retry</Text>
      </TouchableOpacity>
    </View>
  );

  return(
    <ScrollView style={{flex:1,backgroundColor:C.bg}} showsVerticalScrollIndicator={false}>
      <View style={{paddingHorizontal:20,paddingTop:20,paddingBottom:12}}>
        <Text style={{color:C.text,fontSize:34,fontWeight:'800'}}>ML Insights</Text>
        <Text style={{color:C.sub,fontSize:14,marginTop:2}}>Trained on clinical datasets · {scores[0]?.days_of_data??0} days of data</Text>
      </View>
      <View style={{marginHorizontal:16,marginBottom:8,backgroundColor:C.card,borderRadius:12,borderWidth:1,borderColor:C.fill,padding:14}}>
        <Text style={{color:C.orange,fontSize:11,fontWeight:'700',letterSpacing:0.5,marginBottom:6,textTransform:'uppercase'}}>For Wellness Monitoring Only</Text>
        <Text style={{color:C.sub,fontSize:12,lineHeight:18}}>
          These scores identify patterns that may warrant clinical follow-up. They are not medical diagnoses and are not a substitute for professional medical evaluation.
        </Text>
      </View>
      {scores.length>0&&(
        <View style={{marginHorizontal:16,marginBottom:8,backgroundColor:C.card,borderRadius:16,padding:16,flexDirection:'row'}}>
          {[['HIGH',high,C.red],['ELEVATED',elev,C.orange],['LOW',low,C.green]].map(([l,n,c])=>(
            <View key={l as string} style={{flex:1,alignItems:'center'}}>
              <Text style={{color:c as string,fontSize:28,fontWeight:'800'}}>{String(n)}</Text>
              <Text style={{color:C.sub,fontSize:11,marginTop:2}}>{l as string}</Text>
            </View>
          ))}
        </View>
      )}
      {scoredAt&&<Text style={{color:C.muted,fontSize:11,textAlign:'center',marginBottom:12}}>{'Last scored: '+timeAgo(scoredAt)}</Text>}
      {scores.length===0&&(
        <View style={{alignItems:'center',padding:32}}>
          <Text style={{color:C.text,fontSize:16,fontWeight:'700'}}>No scores yet</Text>
          <Text style={{color:C.sub,fontSize:13,marginTop:6}}>Run: python3 nightly_ml_classifier.py</Text>
          <TouchableOpacity style={{backgroundColor:C.blue,paddingHorizontal:20,paddingVertical:10,borderRadius:12,marginTop:16}} onPress={load}>
            <Text style={{color:'#fff',fontWeight:'700'}}>Refresh</Text>
          </TouchableOpacity>
        </View>
      )}
      {scores.map(score=>{
        if(score.insufficient_data)return(
          <View key={score.condition_id} style={{marginHorizontal:16,marginBottom:8,backgroundColor:C.card,borderRadius:14,padding:14,borderLeftWidth:3,borderLeftColor:C.muted}}>
            <Text style={{color:C.text,fontSize:14,fontWeight:'600'}}>{score.condition_label}</Text>
            <Text style={{color:C.muted,fontSize:12,marginTop:4}}>Awaiting sensor data</Text>
          </View>
        );
        const sc=score.score_0_100; const col=lc(sc); const lbl=ll(sc);
        return(
          <View key={score.condition_id} style={{marginHorizontal:16,marginBottom:8,backgroundColor:C.card,borderRadius:14,padding:14,borderLeftWidth:3,borderLeftColor:col}}>
            <View style={{flexDirection:'row',justifyContent:'space-between',alignItems:'flex-start'}}>
              <Text style={{color:C.text,fontSize:14,fontWeight:'600',flex:1,marginRight:8}}>{score.condition_label}</Text>
              <View style={{alignItems:'flex-end'}}>
                <Text style={{color:col,fontSize:24,fontWeight:'800'}}>{String(Math.round(sc))}</Text>
                <Text style={{color:col,fontSize:10,fontWeight:'700',marginTop:1}}>{lbl}</Text>
              </View>
            </View>
            <View style={{height:3,backgroundColor:C.fill,borderRadius:2,marginTop:10}}>
              <View style={{height:3,width:(Math.min(100,Math.max(0,sc))+'%') as any,backgroundColor:col,borderRadius:2}}/>
            </View>
            <View style={{flexDirection:'row',gap:12,marginTop:8}}>
              <Text style={{color:C.muted,fontSize:10}}>{'AUC '+score.published_auc.toFixed(2)}</Text>
              <Text style={{color:C.muted,fontSize:10}}>{String(Math.round(score.data_quality*100))+'% data'}</Text>
              <Text style={{color:C.muted,fontSize:10}}>{String(score.days_of_data)+' days'}</Text>
            </View>
          </View>
        );
      })}
      <View style={{height:40}}/>
    </ScrollView>
  );
}

// ─────────────────────────────────────────────
// MAIN APP
// ─────────────────────────────────────────────
export default function App() {

  // ── Boot state ──
  const [booting,setBooting]           = useState(true);
  const [showOnboarding,setOnboarding] = useState(false);

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
    try{await AX.post(`${url()}/alerts/ack`,{id});}catch(_){}
    setAlerts(p=>p.map(a=>a.id===id?{...a,seen:1}:a));
  },[]);

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
  useEffect(()=>{if(!booting&&!showOnboarding)setTimeout(()=>pingJetson(),1500);},[booting,showOnboarding,pingJetson]);

  useEffect(()=>{
    if(!initialized||jetsonOk!==true)return;
    fetchDashboard();fetchAlerts();loadConfig();fetchRooms();
    setCountdown(AUTO_SYNC_SEC);
    cntTimer.current=setInterval(()=>setCountdown(p=>p<=1?AUTO_SYNC_SEC:p-1),1000);
    autoTimer.current=setInterval(()=>performSync(true),AUTO_SYNC_SEC*1000);
    performSync(true);
    return()=>{if(autoTimer.current)clearInterval(autoTimer.current);if(cntTimer.current)clearInterval(cntTimer.current);};
  },[initialized,jetsonOk]); // eslint-disable-line

  useEffect(()=>{if(!jetsonOk)return;fetchCameraStatus();const id=setInterval(fetchCameraStatus,10000);return()=>clearInterval(id);},[jetsonOk,fetchCameraStatus]);
  useEffect(()=>{if(!jetsonOk)return;fetchCameraSnapshot();const id=setInterval(fetchCameraSnapshot,2500);return()=>clearInterval(id);},[jetsonOk,fetchCameraSnapshot]);
  // Camera-derived metrics (gait speed / cadence / posture) refresh every 4s so they
  // visibly move while someone is walking in frame — the 60s HealthKit auto-sync
  // (above) is far too slow for this to feel live during a walk-test or demo.
  useEffect(()=>{if(!jetsonOk)return;fetchDashboard();const id=setInterval(fetchDashboard,4000);return()=>clearInterval(id);},[jetsonOk,fetchDashboard]);
  useEffect(()=>{if(!jetsonOk)return;const id=setInterval(fetchRooms,15000);return()=>clearInterval(id);},[jetsonOk,fetchRooms]);
  useEffect(()=>{if(!jetsonOk)return;fetchPersons();const id=setInterval(fetchPersons,20000);return()=>clearInterval(id);},[jetsonOk,fetchPersons]);
  // Tightened from 30s -> 6s: this is the "more than one person detected" identification
  // sheet — for a live demo this needs to feel near-instant when a second person walks in.
  useEffect(()=>{if(!jetsonOk)return;fetchPersonAlerts();const id=setInterval(fetchPersonAlerts,6000);return()=>clearInterval(id);},[jetsonOk,fetchPersonAlerts]);
  // Tightened from 15s -> 5s: fall/stillness alerts should reach the UI almost as fast
  // as the SMS itself arrives, not lag up to 15s behind it.
  useEffect(()=>{if(!jetsonOk)return;const id=setInterval(fetchAlerts,5000);return()=>clearInterval(id);},[jetsonOk,fetchAlerts]);

  // ─────────────────────────────────────────────
  // HEALTHKIT SYNC
  // ─────────────────────────────────────────────
  const performSync=useCallback(async(silent=false)=>{
    if(syncInFlight.current||!initialized||jetsonOk!==true)return;
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
            <Text style={{color:C.muted,fontSize:14}}>Live View Unavailable</Text>
            <Text style={{color:C.muted,fontSize:11}}>{cameraStatus?.online?'Camera starting...':'Camera offline'}</Text>
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
      <OnboardingScreen onComplete={(profile,ip,port)=>{
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
    ['insights','ML','◈'],
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
          <PersonDetailScreen
            personId={selectedPerson.id}
            personName={selectedPerson.name??'Unidentified Person'}
            serverUrl={urlRef.current}
            onClose={()=>setSelectedPerson(null)}
          />
        )}
      </Modal>

      {/* Main app */}
      <View style={{flex:1}}>
        {activeTab==='today'&&renderToday()}
        {activeTab==='trends'&&renderTrends()}
        {activeTab==='alerts'&&renderAlerts()}
        {activeTab==='insights'&&<MLInsightsScreen serverUrl={urlRef.current}/>}
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
