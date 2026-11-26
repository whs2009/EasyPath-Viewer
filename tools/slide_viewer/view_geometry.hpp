#pragma once
#include <algorithm>
#include <array>
#include <cmath>
#include <cstdio>
#include <string>
#include <vector>

namespace view {
struct Point {double x=0,y=0;};
inline Point operator+(Point a,Point b){return {a.x+b.x,a.y+b.y};}
inline Point operator-(Point a,Point b){return {a.x-b.x,a.y-b.y};}
inline Point operator*(Point a,double k){return {a.x*k,a.y*k};}
struct Pose {
    Point center;
    int z=12;
    double degrees=0;
};
struct Transform {
    Point center,mid;
    double step,c,s;
    Transform(Pose p,int w,int h):center(p.center),mid{w*.5,h*.5},step(std::ldexp(1.,12-p.z)){
        double a=p.degrees*std::acos(-1.)/180;
        c=std::cos(a);s=std::sin(a);
        if(std::abs(c)<1e-12)c=0;if(std::abs(s)<1e-12)s=0;
    }
    Point inverse_delta(Point d) const {return {(c*d.x+s*d.y)*step,(-s*d.x+c*d.y)*step};}
    Point world(Point screen) const {return center+inverse_delta(screen-mid);}
    Point screen(Point p) const {auto d=(p-center)*(1/step);return mid+Point{c*d.x-s*d.y,s*d.x+c*d.y};}
};
struct Mark {int frame=0,color=0;std::vector<Point> points;};
inline double length(const std::vector<Point>& points,double mx,double my){
    double value=0;
    for(size_t i=1;i<points.size();i++){auto d=points[i]-points[i-1];value+=std::hypot(d.x*mx,d.y*my);}
    return value;
}
inline std::string metric(double um){
    char b[64];
    if(um>=1000)std::snprintf(b,sizeof b,"%.3g mm",um/1000);
    else std::snprintf(b,sizeof b,"%.3g \xc2\xb5m",um);
    return b;
}
struct Scale {double px=0,um=0;};
inline Scale scale(const Transform& t,double mx,double my){
    if(!(mx>0 && my>0))return {};
    auto v=t.inverse_delta({1,0});
    double unit=std::hypot(v.x*mx,v.y*my), max_um=unit*128;
    double base=std::pow(10.,std::floor(std::log10(max_um)));
    double nice=base*(max_um>=5*base?5:max_um>=2*base?2:1);
    return {nice/unit,nice};
}
inline double hit_distance(Point p,Point a,Point b){
    auto d=b-a,q=p-a;
    double n=d.x*d.x+d.y*d.y,t=n?std::clamp((q.x*d.x+q.y*d.y)/n,0.,1.):0.;
    auto v=q-d*t;return std::hypot(v.x,v.y);
}
inline constexpr std::array<unsigned,10> colors{0x1f6fff,0x00a386,0xffd43b,0xe03131,0x18202a,0xffffff,0x7c3aed,0x00bcd4,0xd83bd2,0xff9d2e};
inline constexpr std::array<const char*,10> names{"Blue","Green","Yellow","Red","Black","White","Purple","Cyan","Magenta","Orange"};
}
